"""New navigation batches start with structural and shared-audience needs."""
import copy
import json
import unittest

from backend.scraper.discovery.navigation_order import explain_candidates, order_candidates


ROOT = 'https://example.edu.cn/'


def candidate(suffix, label, kind='navigation', context=None, **extra):
    return ({'url': ROOT + suffix, 'label': label, 'kind': kind, 'path': [], **extra},
            label if context is None else context)


class NavigationAttentionOrderTests(unittest.TestCase):
    def test_root_academic_layout_precedes_organization_and_many_early_edges(self):
        candidates = [candidate('library', '图书馆数据库'), candidate('news', '综合新闻', 'channel'),
                      candidate('org', '组织机构'), candidate('academic', '院系设置')]
        ordered = order_candidates(candidates, {'url': ROOT, 'kind': 'root'})
        self.assertEqual([item[0]['label'] for item in ordered[:2]], ['院系设置', '组织机构'])
        self.assertEqual(len(ordered), len(candidates))

    def test_directory_priority_is_not_a_role_or_identity_change(self):
        item = candidate('opaque', '学科专业设置')
        before = copy.deepcopy(item)
        result = order_candidates([item], {'url': ROOT, 'kind': 'root'})
        self.assertIs(result[0], item)
        self.assertEqual(item, before)
        self.assertEqual(item[0]['kind'], 'navigation')
        self.assertEqual(explain_candidates([item], {'kind': 'root'})[0]['reason'],
                         'observed_academic_layout_hint')

    def test_publication_or_news_is_not_promoted_to_an_academic_directory(self):
        candidates = [candidate('news', '学科建设新闻', 'channel'),
                      candidate('notice', '院系设置', publication_route=True),
                      candidate('org', '组织机构')]
        self.assertEqual(order_candidates(candidates, {'kind': 'root'})[0][0]['label'], '组织机构')

    def test_trunk_hint_is_limited_to_the_root_observation(self):
        candidates = [candidate('aid', '奖学金'), candidate('org', '组织机构', 'directory')]
        self.assertEqual(order_candidates(candidates, {'kind': 'unit'})[0][0]['label'], '奖学金')

    def test_directorys_observed_roster_precedes_repeated_site_navigation(self):
        repeated = candidate('admissions', '研究生招生', locator='#header a')
        college = candidate('college', '化工与环境学院', 'unit', locator='#roster a')
        page = {'kind': 'directory', 'observed_structure': [
            {'url': repeated[0]['url'], 'locator': '#header a', 'relation': 'navigation_entry'},
            {'url': college[0]['url'], 'locator': '#roster a', 'relation': 'directory_entry'},
        ]}
        result = explain_candidates([repeated, college], page)
        self.assertEqual(result[0]['url'], college[0]['url'])
        self.assertEqual(result[0]['reason'], 'observed_directory_member_hint')

    def test_directory_member_hint_needs_exact_url_locator_and_local_relation(self):
        repeated = candidate('admissions', '研究生招生', locator='#header a')
        college = candidate('college', '化工与环境学院', 'unit', locator='#roster a')
        node = {'url': college[0]['url'], 'locator': '#roster a', 'relation': 'directory_entry'}
        for change in ({'url': ROOT + 'other'}, {'locator': '#another-roster a'},
                       {'relation': 'navigation_entry'}):
            with self.subTest(change=change):
                page = {'kind': 'directory', 'observed_structure': [dict(node, **change)]}
                self.assertEqual(order_candidates([repeated, college], page)[0][0]['url'], repeated[0]['url'])

    def test_directory_row_variants_keep_real_teacher_and_student_targets(self):
        repeated = candidate('admissions', '研究生招生', locator='#header a')
        personnel = candidate('staff', '组织人事部', 'unit', locator='#table a')
        unrelated = candidate('audit', '审计处', 'unit', locator='#table audit')
        for relation in ('same_table_row', 'same_directory_row', 'attached_unit',
                         'sub_unit', 'table_reference', 'non_entity_directory_entry'):
            with self.subTest(relation=relation):
                page = {'kind': 'directory', 'observed_structure': [
                    {'url': personnel[0]['url'], 'locator': '#table a', 'relation': relation},
                    {'url': unrelated[0]['url'], 'locator': '#table audit', 'relation': 'directory_entry'},
                ]}
                ordered = order_candidates([unrelated, repeated, personnel], page, {'profiles': ['teacher']})
                self.assertEqual(ordered[0][0]['url'], personnel[0]['url'])
                self.assertEqual(ordered[-1][0]['url'], unrelated[0]['url'])

    def test_bound_roster_inherits_its_academic_directory_context_not_the_header(self):
        repeated = candidate('admissions', '研究生招生', locator='#header a')
        department = candidate('cs', '计算机系', 'unit', locator='#roster a')
        page = {'url': ROOT + 'layout', 'kind': 'directory', 'label': '院系设置',
                'state': 'fetched', 'status_code': 200, 'content_hash': 'a' * 64,
                'observed_structure': [
                    {'url': repeated[0]['url'], 'locator': '#header a', 'relation': 'navigation_entry'},
                    {'url': department[0]['url'], 'locator': '#roster a', 'relation': 'sub_unit'},
                ]}
        explanation = explain_candidates([repeated, department], page)
        self.assertEqual(explanation[0]['url'], department[0]['url'])
        self.assertIn('academic_units', explanation[0]['needs'])
        self.assertNotIn('academic_units', explanation[1]['needs'])

    def test_teacher_and_student_profiles_change_batch_preferences_without_veto(self):
        candidates = [candidate('aid', '奖学金'), candidate('staff', '人事处职称评审'),
                      candidate('research', '科研项目申报')]
        teacher = order_candidates(candidates, {'kind': 'unit'}, {'profiles': ['teacher']})
        student = order_candidates(candidates, {'kind': 'unit'}, {'profiles': ['undergraduate']})
        self.assertEqual(teacher[0][0]['label'], '人事处职称评审')
        self.assertEqual(student[0][0]['label'], '奖学金')
        self.assertEqual({item[0]['url'] for item in teacher}, {item[0]['url'] for item in student})

    def test_shared_whole_menu_text_does_not_taint_every_candidate(self):
        whole_menu = '院系设置 组织机构 奖学金 人事职称 科研项目 就业实习'
        candidates = [candidate('database', '数据检索库', context='学校首页\n' + whole_menu,
                                _navigation_material={'text': whole_menu, 'quote_text': whole_menu}),
                      candidate('aid', '奖学金', context='学校首页\n' + whole_menu),
                      candidate('staff', '人事职称', context='学校首页\n' + whole_menu)]
        results = explain_candidates(candidates, {'kind': 'root'})
        library = next(row for row in results if row['label'] == '数据检索库')
        self.assertEqual(library['needs'], ())
        self.assertEqual(library['score'], 0)
        aid = next(row for row in results if row['label'] == '奖学金')
        self.assertEqual(aid['needs'], ('funding',))

    def test_real_structured_hierarchy_supports_an_unusual_chinese_entrance(self):
        odd = candidate('odd', '树立和践行正确政绩观学习教育', _navigation_material={
            'hierarchy': ['学生资助管理中心', '奖助学金'], 'text': '全站菜单 科研 人事 招生'})
        library = candidate('library', '数据检索库')
        results = explain_candidates([library, odd], {'kind': 'unit'}, {'profiles': ['undergraduate']})
        self.assertEqual(results[0]['url'], odd[0]['url'])
        self.assertIn('funding', results[0]['needs'])
        self.assertNotIn('personnel', results[0]['needs'])
        self.assertIn('observed_ancestry', results[0]['context_basis'])

    def test_isolated_local_heading_can_help_a_generic_more_link(self):
        generic = candidate('opaque', '更多', context='学校首页\n奖学金 更多')
        results = explain_candidates([candidate('library', '数据检索库'), generic], {'kind': 'root'})
        self.assertEqual(results[0]['url'], generic[0]['url'])
        self.assertEqual(results[0]['needs'], ('funding',))
        self.assertTrue(any(item['field'] == 'observed_local_context' for item in results[0]['evidence']))

    def test_mixed_free_context_is_kept_but_not_guessed_as_a_local_scope(self):
        context = '学校首页\n更多 奖学金 科研项目 职称评审'
        item = candidate('opaque', '更多', context=context)
        explanation = explain_candidates([item], {'kind': 'root'})[0]
        self.assertEqual(explanation['needs'], ())
        self.assertIn('mixed_local_context_not_ranked', explanation['context_basis'])
        self.assertEqual(order_candidates([item], {'kind': 'root'})[0][1], context)

    def test_current_unit_identity_is_context_but_root_title_is_not_global_evidence(self):
        item = candidate('opaque', '更多')
        parent = {'url': ROOT + 'funding-centre', 'label': '学生资助管理中心', 'kind': 'unit',
                  'state': 'fetched', 'status_code': 200, 'content_hash': 'a' * 64, 'health': 'date_unknown'}
        self.assertIn('funding', explain_candidates([item], parent)[0]['needs'])
        self.assertEqual(explain_candidates([item], dict(parent, kind='root'))[0]['needs'], ())
        self.assertEqual(explain_candidates([item], dict(parent, health='dynamic_content'))[0]['needs'], ())

    def test_all_candidates_and_duplicate_objects_survive_without_caps(self):
        candidates = [candidate(str(index), '普通入口') for index in range(101)]
        candidates.append(candidates[0])
        frozen = copy.deepcopy(candidates)
        ordered = order_candidates(candidates, {'kind': 'root'})
        self.assertEqual(len(ordered), 102)
        self.assertEqual(ordered, candidates)
        self.assertIs(ordered[-1], candidates[0])
        self.assertEqual(candidates, frozen)

    def test_invalid_preferences_are_explicit_and_explanations_are_json_safe(self):
        with self.assertRaises(ValueError):
            order_candidates([], {'kind': 'root'}, {'profiles': ['typo']})
        result = explain_candidates([candidate('aid', '奖学金')], {'kind': 'root'},
                                    {'weights': {'funding': 2}})
        self.assertEqual(result[0]['score'], 2)
        json.dumps(result, ensure_ascii=False)


if __name__ == '__main__':
    unittest.main()
