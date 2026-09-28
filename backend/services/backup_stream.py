"""Disk-backed JSON rows: a large backup must not become one giant Python object."""
import json
import tempfile

import ijson
from ijson.common import ObjectBuilder


class DiskRows:
    def __init__(self):
        self.file = tempfile.TemporaryFile(mode='w+b')
        self.count = 0
        self.normalized = False

    def append(self, row):
        self.file.write(json.dumps(row, ensure_ascii=False).encode('utf-8') + b'\n')
        self.count += 1

    def __len__(self):
        return self.count

    def __iter__(self):
        self.file.seek(0)
        for line in self.file:
            row = json.loads(line)
            if self.normalized:
                from backend.services.data_transfer import _stamp
                for key in row:
                    if key.endswith('_at'):
                        row[key] = _stamp(row[key])
            yield row

    def __getitem__(self, index):
        if index < 0:
            index += self.count
        for position, row in enumerate(self):
            if position == index:
                return row
        raise IndexError(index)

    def close(self):
        self.file.close()


class BackupData(dict):
    def close(self):
        for rows in self.values():
            if isinstance(rows, DiskRows):
                rows.close()


class ProgressReader:
    def __init__(self, source, total, progress):
        self.source, self.total, self.progress = source, total, progress
        self.done = 0

    def read(self, size=-1):
        chunk = self.source.read(size)
        self.done += len(chunk)
        if self.done > self.total:
            raise ValueError('备份展开大小与清单不一致')
        if self.progress:
            self.progress('reading', self.done, self.total, self.done)
        return chunk


def read_json(source, total, progress=None, max_rows=200000):
    from backend.services.data_transfer import TABLES
    data = BackupData()
    builder = None
    row_depth = 0
    row_size = 0
    seen = set()
    try:
        parser = ijson.parse(ProgressReader(source, total, progress), use_float=True)
        if next(parser, None) != ('', 'start_map', None):
            raise ValueError('备份记录格式无效')
        for prefix, event, value in parser:
            if builder is not None:
                row_size += len(value.encode('utf-8')) if isinstance(value, str) else 8
                if row_size > 12 * 1024 * 1024 or row_depth > 24:
                    raise ValueError('备份中的单条记录过大或嵌套过深')
                builder.event(event, value)
                row_depth += int(event in ('start_map', 'start_array')) - int(event in ('end_map', 'end_array'))
                if row_depth == 0:
                    rows = data[table]
                    rows.append(builder.value)
                    if len(rows) > max_rows:
                        raise ValueError('备份记录数超过本次导入额度，请提高展开大小额度')
                    builder = None
                continue
            if prefix == '' and event == 'map_key':
                if value in seen or value not in {*TABLES, 'format', 'version'}:
                    raise ValueError('备份包含重复或不支持的数据表')
                seen.add(value)
            elif prefix in TABLES:
                if event == 'start_array':
                    data[prefix] = DiskRows()
                elif event != 'end_array':
                    raise ValueError('备份数据表格式无效')
            elif prefix in ('format', 'version') and event in ('string', 'number'):
                data[prefix] = value
            elif prefix.endswith('.item') and prefix[:-5] in TABLES:
                if event != 'start_map':
                    raise ValueError('备份记录格式无效')
                table = prefix[:-5]
                builder = ObjectBuilder()
                builder.event(event, value)
                row_depth, row_size = 1, 0
            elif (prefix, event) != ('', 'end_map'):
                raise ValueError('备份记录格式无效')
        return data
    except Exception as exc:
        data.close()
        if isinstance(exc, (ijson.JSONError, OverflowError)):
            raise ValueError('备份文件损坏或 JSON 格式不支持') from exc
        raise
