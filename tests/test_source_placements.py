"""Published directory evidence fixes presentation without inventing ownership."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import create_app
from backend.database.db import db
from backend.database.models import Department, School
from backend.services.runtime_catalog import RuntimeCatalog, pack
from backend.services.source_inventory import canonical_url, site_key


ROOT = 'https://example.edu.cn/'


def source(ident=21, name='通知公告', group='地球学院', url=ROOT + 'earth/notices/'):
    return SimpleNamespace(id=ident, name=name, group_name=group, list_url=url, school_id=1)


def route(group='院系设置', names=('地球学院',), urls=(ROOT + 'earth/',), reference=None):
    nodes = [{'kind': 'group', 'name': group, 'node_key': 'root',
              'relation': 'page_identity', 'url': reference or ROOT + 'colleges/'}]
    nodes += [{'kind': 'unit', 'name': name, 'node_key': 'node-' + str(i),
               'relation': 'directory_entry', 'url': url}
              for i, (name, url) in enumerate(zip(names, urls))]
    return {'basis': 'official_website_entry', 'nodes': nodes,
            'references': [{'url': reference or ROOT + 'colleges/'}]}


class PlacementEvidenceTests(unittest.TestCase):
    def resolve(self, item, paths, groups=('院系设置', '组织机构', '科学研究')):
        from backend.services.source_placements import placements_from_paths
        return placements_from_paths(item, ROOT, paths, set(groups))

    def test_college_group_is_restored_above_two_stable_columns(self):
        notice = self.resolve(source(), [route()])
        news = self.resolve(source(299, '院内新闻', url=ROOT + 'earth/news/'), [route()])
        self.assertEqual(notice[0]['group'], '院系设置')
        self.assertEqual(notice[0]['nodes'][0]['name'], '地球学院')
        self.assertEqual(notice[0]['nodes'], news[0]['nodes'])
        self.assertEqual([notice[0]['label'], news[0]['label']], ['通知公告', '院内新闻'])

    def test_missing_legacy_group_can_be_restored_from_one_official_path(self):
        self.assertEqual(self.resolve(source(group=''), [route()], groups=())[0]['group'], '院系设置')

    def test_shared_table_caption_is_not_an_extra_unit_and_nested_units_survive(self):
        path = route('组织机构', ('教务部', '创新创业学院'),
                     (ROOT + 'jwb/', ROOT + 'innovation/'))
        path['nodes'].insert(1, {'kind': 'group', 'name': '教务部 / 其他同列单位',
                               'relation': 'shared_table_row', 'node_key': 'caption', 'url': ''})
        result = self.resolve(source(name='教务部-创新创业学院', group='组织机构',
                                     url=ROOT + 'innovation/notices/'), [path])
        self.assertEqual([n['name'] for n in result[0]['nodes']], ['教务部', '创新创业学院'])
        self.assertEqual(result[0]['label'], '本部门通知')

    def test_duplicate_directory_snapshots_share_unit_identity(self):
        a, b = route(), route(reference=ROOT + 'colleges/index.htm')
        b['nodes'][1]['node_key'] = 'different-document-node'
        self.assertEqual(len(self.resolve(source(), [a, b])), 1)

    def test_unknown_cms_source_is_not_matched_by_same_name_without_paths(self):
        self.assertEqual(self.resolve(source(300, '地球学院', '', ROOT + 'zcms/15315/'), []), [])

    def test_school_root_and_school_entry_paths_are_not_reparented(self):
        self.assertEqual(self.resolve(source(url=ROOT), [route()]), [])
        school = {'basis': 'school_website_entry', 'nodes': [], 'references': []}
        self.assertEqual(self.resolve(source(), [route(), school]), [])

    def test_exact_unit_homepage_identity_overrides_school_navigation_cross_link(self):
        school = {'basis': 'school_website_entry', 'nodes': [], 'references': []}
        stranger = route(names=('另一学院',), urls=(ROOT + 'other/',))
        item = source(name='地球学院', group='院系设置', url=ROOT + 'earth/')
        result = self.resolve(item, [school, stranger, route()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['nodes'][-1]['name'], '地球学院')

    def test_name_without_exact_homepage_cannot_override_school_entry(self):
        school = {'basis': 'school_website_entry', 'nodes': [], 'references': []}
        self.assertEqual(self.resolve(source(name='地球学院'), [school, route()]), [])

    def test_cross_links_to_other_units_are_filtered_by_source_scope(self):
        stranger = route(names=('软件学院',), urls=('https://software.example.edu.cn/',))
        result = self.resolve(source(group=''), [route(), stranger])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['nodes'][0]['name'], '地球学院')

    def test_url_prefix_must_end_at_a_path_boundary(self):
        stranger = route(urls=(ROOT + 'ear',))
        self.assertEqual(self.resolve(source(name='招生通知', group=''), [stranger]), [])

    def test_unrelated_subdomain_is_not_accepted_as_the_unit_host(self):
        stranger = route(urls=('https://example.edu.cn.evil.test/earth/',))
        self.assertEqual(self.resolve(source(name='招生通知', group=''), [stranger]), [])

    def test_existing_group_resolves_alternate_directory_page_titles(self):
        alt = route(group='----职能部门----')
        current = route(group='组织机构')
        result = self.resolve(source(group='组织机构'), [alt, current])
        self.assertEqual([p['group'] for p in result], ['组织机构'])

    def test_unseen_admissions_group_is_not_added_to_current_navigation(self):
        extra = route(group='本科招生专业目录')
        result = self.resolve(source(group='院系设置'), [extra, route()])
        self.assertEqual([p['group'] for p in result], ['院系设置'])

    def test_promoted_unit_still_prefers_a_known_official_group(self):
        result = self.resolve(source(), [route(group='本科招生专业目录'), route()])
        self.assertEqual([p['group'] for p in result], ['院系设置'])

    def test_parent_name_does_not_make_its_source_belong_to_a_nested_child(self):
        child_path = route('组织机构', ('教务部', '创新创业学院'),
                           (ROOT + 'jwb/', ROOT + 'innovation/'))
        result = self.resolve(source(name='教务部', group='组织机构', url=ROOT + 'jwb/notices/'),
                              [child_path])
        self.assertEqual(result, [])

    def test_unlinked_promoted_unit_with_conflicting_unknown_roots_stays_unresolved(self):
        paths = [route(group='目录甲'), route(group='目录乙')]
        self.assertEqual(self.resolve(source(), paths, groups=('地球学院',)), [])

    def test_major_nodes_cannot_supply_administrative_ownership(self):
        path = route()
        path['nodes'].append({'kind': 'major', 'name': '地质工程', 'url': '',
                              'relation': 'major_directory_entry', 'node_key': 'major'})
        self.assertEqual(self.resolve(source(), [path]), [])

    def test_multiple_explicit_website_groups_are_preserved(self):
        result = self.resolve(source(group=''), [route(), route(group='科学研究')])
        self.assertEqual({p['group'] for p in result}, {'院系设置', '科学研究'})

    def test_no_flask_context_is_a_safe_legacy_fallback(self):
        from backend.services.source_placements import official_source_placements
        self.assertEqual(official_source_placements([source()]), {})

    def test_unit_bridge_requires_name_group_and_url_scope_together(self):
        from backend.services.source_placements import placements_from_unit_identity
        item = source(name='地球学院', group='院系设置')
        self.assertEqual(placements_from_unit_identity(item, ROOT, [route()], {'院系设置'})[0]['group'], '院系设置')
        for other in [source(name='地球学院', group='组织机构'),
                      source(name='地球学院', group='院系设置', url=ROOT + 'zcms/15315/'),
                      source(name='未知学院', group='院系设置'),
                      source(name='地球学院', group='院系设置', url='https://foreign.edu.cn/earth/')]:
            self.assertEqual(placements_from_unit_identity(other, ROOT, [route()], {'院系设置', '组织机构'}), [])

    def test_ambiguous_unit_identity_cannot_bridge_even_with_matching_prefixes(self):
        from backend.services.source_placements import placements_from_unit_identity
        item = source(name='地球学院', group='院系设置', url=ROOT + 'earth/branch/notices/')
        paths = [route(), route(urls=(ROOT + 'earth/branch/',))]
        self.assertEqual(placements_from_unit_identity(item, ROOT, paths, {'院系设置'}), [])

    def test_one_verified_unit_can_keep_multiple_parent_paths_when_bridged(self):
        from backend.services.source_placements import placements_from_unit_identity
        item = source(name='地球学院', group='院系设置')
        nested = route(names=('教学部', '地球学院'), urls=(ROOT + 'teaching/', ROOT + 'earth/'))
        result = placements_from_unit_identity(item, ROOT, [route(), nested], {'院系设置'})
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['nodes'][-1]['key'], result[1]['nodes'][-1]['key'])


class PlacementCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'catalog.db'
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'placement-test',
                              'SQLALCHEMY_DATABASE_URI': 'sqlite://',
                              'SOURCE_CATALOG_PATH': str(self.path)})
        self.context = self.app.app_context(); self.context.push()
        db.create_all()
        self.school = School(name='例校', url=ROOT)
        self.other = School(name='另一校', url='https://other.edu.cn/')
        db.session.add_all([self.school, self.other]); db.session.flush()
        self.notice = Department(school_id=self.school.id, name='通知公告', group_name='地球学院',
                                 list_url=ROOT + 'earth/notices/')
        self.anchor = Department(school_id=self.school.id, name='物理学院', group_name='院系设置',
                                 list_url=ROOT + 'physics/')
        self.foreign = Department(school_id=self.other.id, name='通知公告', group_name='地球学院',
                                  list_url=ROOT + 'earth/notices/')
        db.session.add_all([self.notice, self.anchor, self.foreign]); db.session.commit()
        cat = RuntimeCatalog(self.path)
        with cat.connect(write=True) as c:
            c.execute('INSERT INTO catalog_sites(site_key,name,root_url,updated_at,report_gzip,paths_gzip,candidates_gzip) '
                      'VALUES(?,?,?,?,?,?,?)', (site_key(ROOT), '例校', ROOT, '2026-09-22', pack({}),
                      pack({canonical_url(self.notice.list_url): [route()]}), pack([])))

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.context.pop(); self.temp.cleanup()

    def test_only_requested_sources_are_returned_and_schools_are_isolated(self):
        from backend.services.source_placements import official_source_placements
        with self.app.test_request_context('/'):
            result = official_source_placements([self.notice, self.foreign])
        self.assertEqual(set(result), {self.notice.id})
        self.assertEqual(result[self.notice.id][0]['group'], '院系设置')

    def test_catalogue_is_inflated_once_per_school_in_one_request(self):
        from backend.services.source_placements import official_source_placements
        original = RuntimeCatalog.paths
        with patch.object(RuntimeCatalog, 'paths', autospec=True, side_effect=original) as read:
            with self.app.test_request_context('/'):
                official_source_placements([self.notice])
                official_source_placements([self.notice, self.anchor])
                self.assertEqual(read.call_count, 1)
            with self.app.test_request_context('/'):
                official_source_placements([self.notice])
                self.assertEqual(read.call_count, 2)

    def test_missing_column_path_can_use_unique_existing_unit_identity(self):
        from backend.services.source_placements import official_source_placements
        own = Department(school_id=self.school.id, name='地球学院', group_name='院系设置',
                         list_url=ROOT + 'earth/older-notices/')
        unknown = Department(school_id=self.school.id, name='地球学院', group_name='院系设置',
                             list_url=ROOT + 'zcms/15315/')
        db.session.add_all([own, unknown]); db.session.commit()
        with self.app.test_request_context('/'):
            result = official_source_placements([own, unknown, self.notice])
        self.assertEqual(set(result), {own.id, self.notice.id})
        self.assertEqual(result[own.id][0]['nodes'], result[self.notice.id][0]['nodes'])

    def test_bridge_cannot_bypass_an_ambiguous_school_entry_path(self):
        from backend.services.source_placements import official_source_placements
        ambiguous = Department(school_id=self.school.id, name='地球学院', group_name='院系设置',
                               list_url=ROOT + 'earth/secondary/')
        db.session.add(ambiguous); db.session.commit()
        paths = {canonical_url(self.notice.list_url): [route()],
                 canonical_url(ambiguous.list_url): [route(),
                    {'basis': 'school_website_entry', 'nodes': [], 'references': []}]}
        with RuntimeCatalog(self.path).connect(write=True) as c:
            c.execute('UPDATE catalog_sites SET paths_gzip=?', (pack(paths),))
        with self.app.test_request_context('/'):
            self.assertEqual(official_source_placements([ambiguous]), {})


if __name__ == '__main__':
    unittest.main()
