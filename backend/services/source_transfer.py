"""Portable source record identities; no source admission or relation inference."""
from collections import defaultdict
import json
import re

from sqlalchemy import inspect

from backend.database.db import db
from backend.database.models import Department
from backend.database.source_governance_models import SourceProposal, SourceTransferIdentity
from backend.services.source_governance import FIELDS, _hash, source_config


def valid_transfer_key(value):
    return isinstance(value, str) and re.fullmatch(r'source:[0-9a-f]{32}', value) is not None


def require_transfer_schema():
    if not inspect(db.session.connection()).has_table(SourceTransferIdentity.__tablename__):
        raise ValueError('信源身份记录尚未升级，请完成程序数据库升级后再导入或导出')


def export_source_keys():
    """Read keys from the same export snapshot; exporting does not create them."""
    require_transfer_schema()
    sources = Department.query.filter_by(kind='column').order_by(Department.id).all()
    keys = {}
    for row in SourceTransferIdentity.query.filter_by(kind='record'):
        if row.source_id in keys or not valid_transfer_key(row.transfer_key):
            raise ValueError('信源身份记录存在冲突，导出未完成')
        keys[row.source_id] = row.transfer_key
    for source in sources:
        if source.id not in keys:
            raise ValueError('信源身份记录缺失，请完成程序数据库升级后再导出')
    return keys


def config_signature(value):
    return _hash({field: value.get(field) or '' for field in FIELDS})


def legacy_record_key(row, school_row):
    """A repeat-import token for this old record, not proof of global identity."""
    return 'legacy:' + _hash(dict(schema=1, school=school_row, record_id=row['id'],
                                  config={field: row.get(field) or '' for field in FIELDS}))


class ImportSourceIdentities:
    def __init__(self):
        require_transfer_schema()
        self.keys = {row.transfer_key: row.source_id for row in SourceTransferIdentity.query.all()}
        self.claimed = set()
        self.review_configs = defaultdict(set)
        for proposal in SourceProposal.query.filter_by(origin='import'):
            try:
                candidate = json.loads(proposal.candidate_json)
                if isinstance(candidate, dict):
                    self.review_configs[proposal.department_id].add(config_signature(candidate))
            except (ValueError, TypeError):
                continue

    def resolve(self, row, school_row, school_id, candidates, structured=None):
        portable = row.get('transfer_key')
        if portable and not valid_transfer_key(portable):
            raise ValueError('备份中的信源身份无效')
        key = portable or legacy_record_key(row, school_row)
        source_id = self.keys.get(key)
        if source_id is not None:
            source = db.session.get(Department, source_id)
            if source is None or source.school_id != school_id or source.kind != 'column':
                raise ValueError('备份信源身份与现有学校或类型冲突，导入未完成')
            if source.id in self.claimed:
                raise ValueError('备份中的多个信源身份指向同一记录，导入未完成')
            if structured is not None and structured.id != source.id:
                raise ValueError('备份中的信源身份与目录身份冲突，导入未完成')
            return source, key
        if portable:
            if structured is not None:
                raise ValueError('备份中的信源身份与目录身份冲突，导入未完成')
            # A foreign stable key is a distinct record, even if all labels match.
            return None, key
        if structured is not None:
            if structured.school_id != school_id or structured.kind != 'column' or structured.id in self.claimed:
                raise ValueError('备份中的信源目录身份冲突，导入未完成')
            return structured, key
        signature = config_signature(row)
        exact = [source for source in candidates if source.id not in self.claimed and (
            config_signature(source_config(source)) == signature or
            signature in self.review_configs[source.id])]
        # A legacy ambiguity stays separate; source CSS alone is not a global ID.
        return (exact[0] if len(exact) == 1 else None), key

    def bind(self, source, key, *, portable):
        if key in self.keys and self.keys[key] != source.id:
            raise ValueError('信源身份映射发生冲突，导入未完成')
        if key not in self.keys:
            existing = db.session.get(SourceTransferIdentity, key)
            if existing is not None and existing.source_id != source.id:
                raise ValueError('信源身份映射发生冲突，导入未完成')
            if existing is None:
                db.session.add(SourceTransferIdentity(source_id=source.id, transfer_key=key,
                                                     kind='record' if portable else 'legacy_record'))
            self.keys[key] = source.id
        self.claimed.add(source.id)
