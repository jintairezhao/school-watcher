"""Observable transfers preserve authentication, import atomicity and resource bounds."""
import io
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import test_storage_management as fixtures
packed = fixtures.packed
from backend.database.db import db
from backend.database.models import Announcement, User
from backend.services.data_transfer import read_backup, merge_data, export_data
from backend.services.backup_stream import DiskRows


class TransferProgressTests(unittest.TestCase):
    setUp = fixtures.StorageManagementTests.setUp
    tearDown = fixtures.StorageManagementTests.tearDown
    client_as = fixtures.StorageManagementTests.client_as
    article = fixtures.StorageManagementTests.article
    sample = fixtures.StorageManagementTests.sample
    def events(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        result = [json.loads(line) for line in response.data.splitlines()]
        response.close()
        return result

    def import_progress(self, file, **limits):
        query = '&'.join(f'{key}={value}' for key, value in limits.items())
        return self.client.post('/api/storage/import?progress=1&' + query,
            data={'file': (file, 'backup.zip')}, headers=self.headers)

    def test_progress_roundtrip_and_protected_download(self):
        self.article('progress')
        events = self.events(self.client.post('/api/storage/export?progress=1', headers=self.headers))
        self.assertEqual(events[-1]['type'], 'result', events)
        phases = [event.get('phase') for event in events]
        self.assertIn('compressing', phases)
        self.assertIn('writing', phases)
        result = events[-1]['result']
        self.assertGreater(result['bytes'], 0)
        other = User(username='other-admin', password_hash='hash', role='admin')
        db.session.add(other); db.session.commit()
        self.assertEqual(self.client_as(other.id).get(result['download_url']).status_code, 404)
        download = self.client.get(result['download_url'])
        self.assertEqual(download.status_code, 200)
        stream = io.BytesIO(download.data); download.close()
        events = self.events(self.import_progress(stream))
        self.assertEqual(events[-1]['result']['duplicates'], 1, events)
        self.assertIn('committing', [event.get('phase') for event in events])
        self.assertEqual(Announcement.query.count(), 1)

    def test_desktop_export_writes_complete_zip_to_selected_directory(self):
        self.app.config['DESKTOP_MODE'] = True
        self.app.config['DESKTOP_TOKEN'] = 'x' * 32
        self.app.config['DESKTOP_ORIGIN'] = 'http://localhost'
        with self.client.session_transaction() as session:
            session['desktop_access'] = 'x' * 32
        self.article('desktop')
        with patch('backend.auth.desktop.local_owner', return_value=self.admin):
            events = self.events(self.client.post('/api/storage/export?progress=1&save=1', headers=self.headers))
        result = events[-1]['result']
        self.assertTrue(result['saved'])
        path = Path(self.app.config['BACKUP_DIR']) / result['name']
        self.assertEqual(path.stat().st_size, result['bytes'])
        with path.open('rb') as stream:
            self.assertEqual(len(read_backup(stream)['announcements']), 1)
        self.assertFalse(list(path.parent.glob('*.part')))

    def test_streaming_import_rejects_expansion_before_writing_and_can_retry(self):
        data = self.sample()
        data['announcements'][0]['content_text'] = 'a' * (1024 * 1024 + 8)
        events = self.events(self.import_progress(packed(data), expanded_mb=1))
        self.assertEqual(events[-1]['type'], 'error')
        self.assertIn('额度', events[-1]['error'])
        self.assertEqual(Announcement.query.count(), 0)
        events = self.events(self.import_progress(packed(data), expanded_mb=3))
        self.assertEqual(events[-1]['result']['added'], 1, events)

    def test_streaming_merge_failure_rolls_back_and_retry_works(self):
        with patch('backend.services.data_transfer._merge_source', side_effect=RuntimeError('fixture failure')):
            events = self.events(self.import_progress(packed(self.sample())))
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(Announcement.query.count(), 0)
        events = self.events(self.import_progress(packed(self.sample())))
        self.assertEqual(events[-1]['result']['added'], 1, events)

    def test_stream_parser_disk_rows_dates_and_html_safety(self):
        source = self.sample()
        source['announcements'][0]['content_html'] = '<script>alert(1)</script><p onclick="evil()">ok</p>'
        phases = []
        data = read_backup(packed(source), lambda *event: phases.append(event), disk_backed=True)
        try:
            self.assertIsInstance(data['announcements'], DiskRows)
            self.assertEqual(data['announcements'][0]['published_at'].year, 2020)
            self.assertEqual(merge_data(data)['added'], 1)
            self.assertNotIn('script', Announcement.query.one().content_html)
            self.assertNotIn('onclick', Announcement.query.one().content_html)
            self.assertTrue(any(item[0] == 'reading' and item[1] == item[2] for item in phases))
        finally:
            data.close()

    def test_real_file_over_100mb_imports_with_selected_budget(self):
        # Valid JSON whitespace makes a real >100 MB file without allocating it
        # in RAM or stuffing the fixture database with meaningless large bodies.
        with tempfile.TemporaryFile('w+b') as stream:
            with zipfile.ZipFile(stream, 'w', zipfile.ZIP_STORED) as archive:
                with archive.open('data.json', 'w') as out:
                    for _ in range(101):
                        out.write(b' ' * 1048576)
                    out.write(json.dumps(self.sample()).encode())
            self.assertGreater(stream.tell(), 100 * 1048576)
            stream.seek(0)
            events = self.events(self.import_progress(stream, upload_mb=104, expanded_mb=120))
        self.assertEqual(events[-1]['result']['added'], 1, events)

    def test_progress_endpoints_require_admin_csrf_and_valid_limits(self):
        reader = self.client_as(self.reader.id)
        for path in ('/api/storage/export?progress=1', '/api/storage/import?progress=1'):
            self.assertEqual(reader.post(path, headers=self.headers).status_code, 403)
            self.assertEqual(self.client.post(path).status_code, 403)
        for value in ('0', '-1', 'nan', '1.5', '99999999999999999999'):
            response = self.import_progress(packed(self.sample()), expanded_mb=value)
            self.assertEqual(response.status_code, 400)

    def test_duplicate_tables_and_nested_invalid_data_are_rejected(self):
        raw = json.dumps(self.sample())[:-1] + ',"schools":[]}'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('data.json', raw)
        stream.seek(0)
        events = self.events(self.import_progress(stream))
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(Announcement.query.count(), 0)

    def test_disconnect_before_commit_rolls_back_import(self):
        from backend.services.transfer_progress import TransferDisconnected
        data = read_backup(packed(self.sample()), disk_backed=True)
        def disconnected(phase, *values):
            if phase == 'committing':
                raise TransferDisconnected()
        try:
            with self.assertRaises(TransferDisconnected):
                merge_data(data, disconnected)
            self.assertEqual(Announcement.query.count(), 0)
        finally:
            data.close()

    def test_legacy_database_uses_disk_rows_and_closes_them(self):
        import sqlite3
        self.article('legacy')
        target = self.root / 'snapshot.db'
        with closing(sqlite3.connect(self.root / 'main.db')) as source, closing(sqlite3.connect(target)) as output:
            source.backup(output)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.write(target, 'school_watcher.db')
            archive.writestr('manifest.json', json.dumps({'school_watcher.db':target.stat().st_size}))
        stream.seek(0)
        data = read_backup(stream, disk_backed=True)
        try:
            self.assertIsInstance(data['announcements'], DiskRows)
            self.assertEqual(merge_data(data)['duplicates'], 1)
        finally:
            data.close()
        self.assertTrue(data['announcements'].file.closed)


if __name__ == '__main__':
    unittest.main()
