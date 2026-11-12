"""Published directory material and typed memberships commit as one change."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from sqlalchemy import event

from backend.database.db import db
from backend.database.models import CatalogPublication, Department, DepartmentDirectoryEntry, Subscription
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.school_structure import sync_official_structure
from tests import test_school_structure as fixture


class AtomicStructurePublicationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SchoolStructureTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def pointer(self):
        return db.session.get(CatalogPublication, self.f.key).generation

    def test_successful_publication_already_contains_typed_memberships(self):
        self.f.publish()
        rows = DepartmentDirectoryEntry.query.all()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row.facts_json)
            for fact in row.facts_json:
                self.assertEqual(fact['type'], 'directory_listing')
                self.assertTrue(all(proof['reference_url'] and proof['locator'] and
                    proof['content_hash'] and proof['observed_at'] for proof in fact['evidence']))

    def test_sync_failure_rolls_back_memberships_and_keeps_old_pointer(self):
        self.f.publish()
        before = self.pointer()
        subscriptions = [(s.id, s.department_ids) for s in Subscription.query.all()]
        self.f.inventory.finish(self.f.key, fixture.ROOT, state='fetched', html='<p>new material</p>')
        def broken_sync(school, catalog, *, commit):
            self.assertFalse(commit)
            db.session.add(Department(school_id=school.id, name='MUST ROLLBACK', kind='unit'))
            db.session.flush()
            raise RuntimeError('injected membership write failure')
        with patch('backend.services.school_structure.sync_official_structure', side_effect=broken_sync):
            with self.assertRaisesRegex(RuntimeError, 'membership write failure'):
                self.f.catalog.publish(self.f.inventory, self.f.key)
        self.assertEqual(self.pointer(), before)
        self.assertIsNone(Department.query.filter_by(name='MUST ROLLBACK').first())
        self.assertEqual(subscriptions, [(s.id, s.department_ids) for s in Subscription.query.all()])

    def test_pointer_and_memberships_have_only_one_visible_commit(self):
        # Observe with an independent read-only connection after each commit;
        # no intermediate pointer-only transaction is permitted.
        captured = []
        session = db.session()
        path = Path(db.engine.url.database)
        def after_commit(_):
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as reader:
                generation = reader.execute('SELECT generation FROM catalog_publications WHERE site_key=?', (self.f.key,)).fetchone()
                count = reader.execute("SELECT count(*) FROM department_directory_entries WHERE facts_json != '[]'").fetchone()[0]
                captured.append((generation[0] if generation else None, count))
        event.listen(session, 'after_commit', after_commit)
        try:
            for url, kind, label in [(fixture.ROOT, 'root', '测试大学'), (fixture.ROOT + 'colleges/', 'directory', '院系设置')]:
                self.f.inventory.enqueue(self.f.key, url, label, kind, 1, [], 'school_domain')
                inspect_page(self.f.inventory, self.f.inventory.report(self.f.key)['site'],
                    self.f.inventory.get_page(self.f.key, url),
                    fetcher=lambda address: {'url': address, 'status': 200, 'html': self.f.pages[address]})
            generation = self.f.catalog.publish(self.f.inventory, self.f.key)
        finally:
            event.remove(session, 'after_commit', after_commit)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], generation)
        self.assertGreater(captured[0][1], 0)

    def test_pin_stays_on_old_file_and_unchanged_publish_reuses_generation(self):
        self.f.publish()
        before = self.pointer()
        pinned = self.f.catalog.pin(self.f.key)
        old_page = pinned.get_page(self.f.key, fixture.ROOT)
        self.assertEqual(self.f.catalog.publish(self.f.inventory, self.f.key), before)
        self.f.inventory.finish(self.f.key, fixture.ROOT, state='fetched', html='<p>replacement</p>')
        after = self.f.catalog.publish(self.f.inventory, self.f.key)
        self.assertNotEqual(before, after)
        self.assertEqual(pinned.get_page(self.f.key, fixture.ROOT), old_page)
        self.assertNotEqual(self.f.catalog.pin(self.f.key).get_page(self.f.key, fixture.ROOT)['content_hash'], old_page['content_hash'])

    def test_new_execution_events_publish_with_their_generation_and_structure_version(self):
        from backend.services.structure_versions import current_version
        self.f.publish()
        pinned = self.f.catalog.pin(self.f.key)
        old_events = pinned.monitor_snapshot(self.f.key)['exploration_events']
        before = current_version(self.f.school.id)
        page = self.f.inventory.get_page(self.f.key, fixture.ROOT)
        # Same HTML and business entities, but a genuine new inspection occurred.
        inspect_page(self.f.inventory, self.f.inventory.report(self.f.key)['site'], page,
            fetcher=lambda url: {'url': url, 'status': 200, 'html': self.f.pages[url]}, ai_assist=False)
        self.f.catalog.publish(self.f.inventory, self.f.key)
        after = current_version(self.f.school.id)
        self.assertEqual(after['revision'], before['revision'] + 1)
        self.assertEqual(after['epoch'], before['epoch'])
        self.assertEqual(pinned.monitor_snapshot(self.f.key)['exploration_events'], old_events)
        new_events = self.f.catalog.pin(self.f.key).monitor_snapshot(self.f.key)['exploration_events']
        self.assertEqual(len(new_events), len(old_events) + 2)
        self.assertEqual(self.f.catalog.publish(self.f.inventory, self.f.key), self.pointer())
        self.assertEqual(current_version(self.f.school.id), after)

    def test_nested_directory_is_not_promoted_to_organization(self):
        self.f.pages[fixture.ROOT + 'colleges/'] = (
            '<main><h1>院系设置</h1><ul><li><span>科学学院</span><ul><li><a href="' + fixture.CS + '">'
            '计算机学院</a></li></ul></li></ul></main>')
        self.f.publish()
        parent = Department.query.filter_by(name='科学学院', kind='unit').one()
        child = Department.query.filter_by(name='计算机学院', kind='unit').one()
        row = db.session.get(DepartmentDirectoryEntry, (parent.id, child.id))
        self.assertEqual({f['type'] for f in row.facts_json}, {'directory_listing'})

    def test_explicit_subunit_table_retains_distinct_relationship_type(self):
        self.f.pages[fixture.ROOT + 'colleges/'] = ('<h1>院系设置</h1><table><tr><th>学院</th><th>下属单位</th></tr>'
            '<tr><td>科学学院</td><td><a href="' + fixture.CS + '">计算机学院</a></td></tr></table>')
        self.f.publish()
        parent = Department.query.filter_by(name='科学学院', kind='unit').one()
        child = Department.query.filter_by(name='计算机学院', kind='unit').one()
        facts = db.session.get(DepartmentDirectoryEntry, (parent.id, child.id)).facts_json
        self.assertTrue(any(f['type'] == 'organization_membership' and f['observed_relation'] == 'sub_unit' for f in facts))
        self.assertTrue(all(f['assertion']['text'] == '下属单位' and f['assertion']['direction'] == 'parent_to_child' for f in facts))

    def test_reverse_ownership_header_is_not_promoted_to_forward_organization(self):
        self.f.pages[fixture.ROOT + 'colleges/'] = ('<h1>院系设置</h1><table><tr><th>学院</th><th>所属单位</th></tr>'
            '<tr><td>科学学院</td><td><a href="' + fixture.CS + '">计算机学院</a></td></tr></table>')
        self.f.publish()
        self.assertFalse(any(f['type'] == 'organization_membership'
            for row in DepartmentDirectoryEntry.query.all() for f in row.facts_json))

    def test_missing_retained_material_cannot_promote_a_parser_claim(self):
        self.f.pages[fixture.ROOT + 'colleges/'] = ('<h1>院系设置</h1><table><tr><th>学院</th><th>下属单位</th></tr>'
            '<tr><td>科学学院</td><td><a href="' + fixture.CS + '">计算机学院</a></td></tr></table>')
        with patch.object(self.f.inventory, 'snapshot', return_value=''):
            self.f.publish()
        self.assertFalse(any(f['type'] == 'organization_membership'
            for row in DepartmentDirectoryEntry.query.all() for f in row.facts_json))

    def test_publication_owner_requires_executed_receipt_and_cross_link_does_not_grant_owner(self):
        self.f.publish()
        result = self.f.connect(fixture.CS + 'notices/')
        source_id = result['department_ids'][0]
        rows = DepartmentDirectoryEntry.query.filter_by(department_id=source_id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(db.session.get(Department, rows[0].parent_id).name, '计算机学院')
        facts = rows[0].facts_json
        self.assertTrue(any(f['type'] == 'source_ownership' and f['receipt_id'] and f['config_hash'] for f in facts))
        self.assertTrue(any(f.get('assertion', {}).get('kind') == 'explicit_publisher_identity' for f in facts))

    def test_home_shortcut_does_not_suppress_the_proven_department_publisher(self):
        self.f.pages[fixture.ROOT] += '<nav><a href="' + fixture.CS + 'notices/">学院通知直达</a></nav>'
        self.f.publish()
        from backend.services.runtime_catalog import relationships_for
        paths = relationships_for(self.f.catalog, self.f.key).paths_for(fixture.CS + 'notices/')
        self.assertTrue(any(path['basis'] == 'school_website_entry' and not path['nodes'] for path in paths))
        self.assertTrue(any(path['basis'] == 'official_website_entry' and path['unit_name'] == '计算机学院' for path in paths))
        source_id = self.f.connect(fixture.CS + 'notices/')['department_ids'][0]
        owners = [(db.session.get(Department, row.parent_id).name, fact)
                  for row in DepartmentDirectoryEntry.query.filter_by(department_id=source_id)
                  for fact in row.facts_json if fact['type'] == 'source_ownership']
        self.assertEqual({name for name, fact in owners}, {'计算机学院'})
        self.assertTrue(all(fact.get('receipt_id') and fact.get('assertion') for name, fact in owners))

    def test_successful_collection_receipt_without_retained_identity_is_not_ownership(self):
        with patch.object(self.f.inventory, 'snapshot', return_value=''):
            self.f.publish()
        result = self.f.connect(fixture.CS + 'notices/')
        source_id = result['department_ids'][0]
        rows = DepartmentDirectoryEntry.query.filter_by(department_id=source_id).all()
        self.assertTrue(rows)
        self.assertFalse(any(f['type'] == 'source_ownership' for row in rows for f in row.facts_json))
        self.assertTrue(all(f['type'] == 'directory_listing' for row in rows for f in row.facts_json))

    def test_successful_removal_retracts_owned_facts_but_retains_legacy_memberships_and_unit_ids(self):
        self.f.publish()
        unit = Department.query.filter_by(name='计算机学院', kind='unit').one()
        ident = unit.id
        legacy = Department(school_id=self.f.school.id, name='Legacy grouping', kind='group')
        db.session.add(legacy); db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=legacy.id, department_id=ident))
        db.session.commit()
        address = fixture.ROOT + 'colleges/'
        self.f.pages[address] = '<main><h1>院系设置</h1><p>目录更新中</p></main>'
        inspect_page(self.f.inventory, self.f.inventory.report(self.f.key)['site'],
            self.f.inventory.get_page(self.f.key, address),
            fetcher=lambda url: {'url': url, 'status': 200, 'html': self.f.pages[url]})
        self.f.catalog.publish(self.f.inventory, self.f.key, merge=True)
        memberships = DepartmentDirectoryEntry.query.filter_by(department_id=ident).all()
        self.assertEqual([(e.parent_id, e.facts_json) for e in memberships], [(legacy.id, [])])
        self.assertIsNotNone(db.session.get(Department, ident))

    def test_failed_reread_retains_last_good_typed_membership(self):
        self.f.publish()
        before = {(r.parent_id, r.department_id): r.facts_json for r in DepartmentDirectoryEntry.query.all()}
        self.f.inventory.finish(self.f.key, fixture.ROOT + 'colleges/', state='failed', error='network unavailable')
        self.f.catalog.publish(self.f.inventory, self.f.key, merge=True)
        self.assertEqual(before, {(r.parent_id, r.department_id): r.facts_json for r in DepartmentDirectoryEntry.query.all()})

    def test_distinct_roster_observations_do_not_merge_units_by_name_and_url(self):
        self.f.publish()
        address = fixture.ROOT + 'research/'
        html = '<main><h1>研究机构</h1><ul><li><a href="' + fixture.CS + '">计算机学院</a></li></ul></main>'
        self.f.inventory.enqueue(self.f.key, address, '研究机构', 'directory', 1, [], 'school_domain')
        inspect_page(self.f.inventory, self.f.inventory.report(self.f.key)['site'],
            self.f.inventory.get_page(self.f.key, address),
            fetcher=lambda url: {'url': url, 'status': 200, 'html': html})
        self.f.catalog.publish(self.f.inventory, self.f.key, merge=True)
        units = Department.query.filter_by(name='计算机学院', kind='unit').all()
        self.assertEqual(len(units), 2)
        identities = {unit.structure_key for unit in units}
        self.assertEqual(len(identities), 2)
        self.f.catalog.publish(self.f.inventory, self.f.key, merge=True)
        self.assertEqual(identities, {unit.structure_key for unit in Department.query.filter_by(name='计算机学院', kind='unit')})

    def test_new_material_retracts_directory_options_copy_in_same_publication(self):
        self.f.publish()
        from backend.services.directory_options import sync_directory_options
        sync_directory_options(self.f.school, self.f.catalog)
        self.assertTrue(any(f.get('writer') == 'directory_options'
            for row in DepartmentDirectoryEntry.query.all() for f in row.facts_json))
        ident = Department.query.filter_by(name='计算机学院', kind='unit').one().id
        address = fixture.ROOT + 'colleges/'
        inspect_page(self.f.inventory, self.f.inventory.report(self.f.key)['site'],
            self.f.inventory.get_page(self.f.key, address),
            fetcher=lambda url: {'url': url, 'status': 200, 'html': '<main><h1>院系设置</h1><p>目录更新中</p></main>'})
        self.f.catalog.publish(self.f.inventory, self.f.key, merge=True)
        self.assertEqual(DepartmentDirectoryEntry.query.filter_by(department_id=ident).count(), 0)
        self.assertIsNotNone(db.session.get(Department, ident))


class DirectoryEvidenceMigrationTests(unittest.TestCase):
    def test_upgrade_keeps_legacy_pairs_and_marks_them_without_evidence(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, text
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/16c8d9234ba1_directory_relation_evidence.py'
        spec = importlib.util.spec_from_file_location('directory_evidence_migration', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE department_directory_entries (parent_id INTEGER, department_id INTEGER, position INTEGER, PRIMARY KEY(parent_id,department_id))'))
            connection.execute(text('INSERT INTO department_directory_entries VALUES (7,11,2),(8,11,1)'))
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            rows = connection.execute(text('SELECT * FROM department_directory_entries ORDER BY parent_id')).fetchall()
            self.assertEqual([tuple(row)[:3] for row in rows], [(7, 11, 2), (8, 11, 1)])
            self.assertEqual([json.loads(row[3]) for row in rows], [[], []])
        engine.dispose()


if __name__ == '__main__':
    unittest.main()
