"""A common website cannot make a college column belong to every child unit."""
import json
from pathlib import Path
import unittest
from types import SimpleNamespace

from backend.services.source_placements import placements_from_paths


class SharedWebsitePlacementTests(unittest.TestCase):
    def test_real_retained_college_columns_are_not_broadcast_to_child_departments(self):
        data = json.loads((Path(__file__).parent/'acceptance/shared_website_placements_20261016.json').read_text('utf-8'))
        for sample in data['samples']:
            with self.subTest(source=sample['source']['list_url']):
                result = placements_from_paths(SimpleNamespace(**sample['source']), sample['school_url'],
                    sample['observed_paths'], set(sample['known_groups']))
                self.assertEqual([[node['name'] for node in place['nodes']] for place in result],
                                 sample['expected_unit_paths'])

    def test_observed_specific_child_column_keeps_its_own_path(self):
        from tests.test_source_placements import source, route, ROOT
        parent = route(names=('甲学院',), urls=(ROOT+'shared/',))
        child = route(names=('甲学院','乙系'), urls=(ROOT+'shared/',ROOT+'shared/'))
        for item in (source(name='乙系-通知公告', group='院系设置', url=ROOT+'shared/notices/'),
                     source(name='通知公告', group='乙系', url=ROOT+'shared/notices/')):
            result = placements_from_paths(item,ROOT,[parent,child],{'院系设置'})
            self.assertEqual([[n['name'] for n in p['nodes']] for p in result],[['甲学院','乙系']])

    def test_shared_sibling_home_without_owner_evidence_stays_unresolved(self):
        from tests.test_source_placements import source, route, ROOT
        paths = [route(names=(name,), urls=(ROOT+'shared/',)) for name in ('甲学院','乙学院')]
        self.assertEqual(placements_from_paths(source(group='',url=ROOT+'shared/notices/'),ROOT,paths,{'院系设置'}),[])
        resolved = placements_from_paths(source(group='乙学院',url=ROOT+'shared/notices/'),ROOT,paths,{'院系设置'})
        self.assertEqual([[n['name'] for n in p['nodes']] for p in resolved],[['乙学院']])

    def test_child_subscription_does_not_expand_to_a_college_wide_column(self):
        from tests.test_source_placements import PlacementCatalogueTests, ROOT, route
        from backend.database.db import db
        from backend.database.models import Department, DepartmentDirectoryEntry
        from backend.services.runtime_catalog import RuntimeCatalog, pack
        from backend.services.source_inventory import canonical_url, site_key
        from backend.services.directory_options import expand_directory_ids
        fixture = PlacementCatalogueTests()
        fixture.setUp()
        try:
            fixture.notice.list_selector = 'li'
            parent = Department(school_id=fixture.school.id, kind='unit', name='地球学院',
                                group_name='院系设置', list_url=ROOT+'earth/')
            child = Department(school_id=fixture.school.id, kind='unit', name='地球物理系',
                               group_name='院系设置', list_url=ROOT+'earth/')
            db.session.add_all([parent, child]); db.session.flush()
            db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=child.id, position=0))
            # An older published graph may already have saved the broad column
            # beneath this child. Current ownership must also govern expansion.
            db.session.add(DepartmentDirectoryEntry(parent_id=child.id, department_id=fixture.notice.id, position=0))
            db.session.commit()
            paths = [route(), route(names=('地球学院','地球物理系'), urls=(ROOT+'earth/',ROOT+'earth/'))]
            with RuntimeCatalog(fixture.path).connect(write=True) as conn:
                conn.execute('UPDATE catalog_sites SET paths_gzip=? WHERE site_key=?',
                    (pack({canonical_url(fixture.notice.list_url):paths}), site_key(ROOT)))
            before = (fixture.notice.id,fixture.notice.name,fixture.notice.list_url,Department.query.count())
            with fixture.app.test_request_context('/'):
                self.assertNotIn(fixture.notice.id, expand_directory_ids(fixture.school.id,[child.id]))
                self.assertIn(fixture.notice.id, expand_directory_ids(fixture.school.id,[parent.id]))
                self.assertIn(fixture.notice.id, expand_directory_ids(fixture.school.id,[fixture.notice.id]))
            self.assertEqual((fixture.notice.id,fixture.notice.name,fixture.notice.list_url,Department.query.count()),before)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
