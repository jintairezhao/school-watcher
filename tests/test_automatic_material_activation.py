"""Discovery queue to automatic source installation retains directory proof."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from sqlalchemy import event

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, CatalogPublication, Department, DepartmentDirectoryEntry
from backend.database.source_governance_models import SourceConfigVersion, SourceValidation
from backend.services import source_governance as governance, source_onboarding as onboarding
from backend.services.structure_versions import current_version
from backend.services.structure_snapshot import read_structure, graph_projection, source_choices
from tests import test_source_material_activation as material_case
from tests import test_school_structure as fixture


class AutomaticMaterialActivationTests(unittest.TestCase):
    def setUp(self):
        self.case = material_case.SourceMaterialActivationTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.f = self.case.f

    def queue(self):
        onboarding.queue_columns(self.f.school.id, self.f.inventory, self.f.key)
        return next(row for row in BackgroundTask.query.filter_by(kind='onboard')
                    if row.payload['url'] == fixture.CS + 'notices/')

    def run_job(self, job):
        return onboarding.onboard_page(job.payload, fetcher=lambda url, purpose:
            self.f.fixture.fetch(url, purpose) if purpose in ('body', 'article') else fixture.HTML)

    def owners(self, source_id):
        return [fact for row in DepartmentDirectoryEntry.query.filter_by(department_id=source_id)
                for fact in row.facts_json if fact['type'] == 'source_ownership']

    def test_queue_to_install_commits_material_config_owner_and_notices_together(self):
        job = self.queue()
        material = job.payload['structure_material']
        self.assertTrue(any(r['url'] == fixture.ROOT + 'colleges/' for r in material['identity_snapshots']))
        self.assertTrue(all(r['captured_at'] == self.case.stamp for r in material['identity_snapshots']))
        self.assertEqual(CatalogPublication.query.count(), 0)
        before = current_version(self.f.school.id)
        commits = []
        database = Path(db.engine.url.database).resolve().as_uri() + '?mode=ro'
        def committed(_):
            with closing(sqlite3.connect(database, uri=True)) as reader:
                facts = [f for row in reader.execute('select facts_json from department_directory_entries') for f in json.loads(row[0])]
                commits.append(dict(pointer=reader.execute('select count(*) from catalog_publications').fetchone()[0],
                    notices=reader.execute('select count(*) from announcements').fetchone()[0],
                    owner=any(f['type'] == 'source_ownership' for f in facts)))
        original = onboarding._install
        def observed_install(*args, **kwargs):
            session = db.session()
            event.listen(session, 'after_commit', committed)
            try:
                return original(*args, **kwargs)
            finally:
                event.remove(session, 'after_commit', committed)
        with patch.object(onboarding, '_install', side_effect=observed_install), \
                patch('backend.ai.runtime.run_skill', side_effect=AssertionError('no models')):
            result = self.run_job(job)
        self.assertEqual(result['state'], 'connected', result)
        self.assertEqual(commits, [dict(pointer=1, notices=3, owner=True)])
        ident = result['department_ids'][0]
        self.assertTrue(self.owners(ident))
        self.assertEqual(current_version(self.f.school.id)['revision'], before['revision'] + 1)
        view = read_structure(self.f.school)
        graph = graph_projection(view, self.f.school)
        self.assertTrue(any(r['type'] == 'source_ownership' and r['target'] == f'source:{ident}' for r in graph['relations']))
        self.assertTrue(any(g['name'] == '计算机学院' and any(c['department'].id == ident for c in g['columns'])
            for g in source_choices(view)['有依据的栏目归属']))
        receipt = SourceValidation.query.filter_by(department_id=ident).one()
        self.assertEqual(json.loads(receipt.validation_json)['structure_material']['material_hash'], material['material_hash'])
        self.assertEqual(self.f.catalog.pin(self.f.key).monitor_snapshot(self.f.key)['exploration_events'], [])
        saved_version = current_version(self.f.school.id)
        pointer = db.session.get(CatalogPublication, self.f.key).generation
        again = self.run_job(job)
        self.assertEqual(again['department_ids'], [ident])
        self.assertEqual(SourceConfigVersion.query.count(), 1)
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(SourceValidation.query.filter_by(department_id=ident).count(), 1)
        self.assertEqual(current_version(self.f.school.id), saved_version)
        self.assertEqual(db.session.get(CatalogPublication, self.f.key).generation, pointer)

    def test_pointer_failure_rolls_back_notices_receipt_configuration_and_owner(self):
        job = self.queue()
        before = current_version(self.f.school.id)
        def reject_pointer(connection, cursor, statement, parameters, context, many):
            if statement.lower().startswith('insert into catalog_publications'):
                raise RuntimeError('automatic pointer failure')
        event.listen(db.engine, 'before_cursor_execute', reject_pointer)
        try:
            with self.assertRaisesRegex(RuntimeError, 'automatic pointer failure'):
                self.run_job(job)
        finally:
            event.remove(db.engine, 'before_cursor_execute', reject_pointer)
        db.session.commit()
        self.assertEqual(CatalogPublication.query.count(), 0)
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertEqual(SourceValidation.query.count(), 0)
        self.assertEqual(Announcement.query.count(), 0)
        self.assertEqual(current_version(self.f.school.id), before)
        self.assertTrue(self.run_job(job)['department_ids'])

    def test_missing_roster_raw_material_remains_an_ownership_gap(self):
        url = fixture.ROOT + 'colleges/'
        page = self.f.inventory.get_page(self.f.key, url)
        with self.f.inventory.connect() as writer:
            writer.execute('DELETE FROM snapshots WHERE site_key=? AND url=?', (self.f.key, url))
        (governance._evidence_root() / (page['content_hash'] + '.html.gz')).unlink()
        job = self.queue()
        self.assertIn(dict(url=url, hash=page['content_hash'], reason='raw_material_unavailable'),
            job.payload['structure_material']['material_gaps'])
        result = self.run_job(job)
        self.assertEqual(result['state'], 'connected', result)
        self.assertFalse(self.owners(result['department_ids'][0]))
        self.assertFalse(Department.query.filter_by(kind='unit', name='计算机学院').count())

    def test_changed_material_envelope_is_rejected_before_install(self):
        job = self.queue()
        payload = dict(job.payload)
        payload['structure_material']['identity_site']['name'] = 'corrupted material'
        with self.assertRaisesRegex(ValueError, '结构材料与已验证栏目不一致'):
            onboarding.onboard_page(payload, fetcher=lambda url, purpose:
                self.f.fixture.fetch(url, purpose) if purpose in ('body', 'article') else fixture.HTML)
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertEqual(CatalogPublication.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
