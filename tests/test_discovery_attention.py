"""Audience demand helps find sources; it must not invent or exclude them."""
import json
from types import SimpleNamespace
import unittest

from backend.scraper.discovery.attention import (
    ALL_PROFILES, CATEGORY_KEYS, candidate_attention, coverage_state,
    need_evidence, normalize_profiles,
)


ROOT = 'https://example.edu.cn/'
HASH = 'a' * 64
SECOND_HASH = 'b' * 64
THIRD_HASH = 'c' * 64


def page(suffix, label, **values):
    result = dict(url=ROOT + suffix, label=label, kind='channel', path_json='[]',
                  status_code=200, health='ready')
    result.update(values)
    return result


def receipt(item, source_id=1, region='#notices', **values):
    return dict(url=item['url'], source_id=source_id, config_hash='actual-config',
                content_hash=item.get('content_hash'), region=region, **values)


class DiscoveryAttentionTests(unittest.TestCase):
    def native_receipt(self, item, region):
        from backend.services.source_path_receipts import _native_receipt
        source = SimpleNamespace(id=8, school_id=17)
        event = SimpleNamespace(id=22, department_id=source.id, config_hash=SECOND_HASH,
            validation_json=json.dumps({'status': 'full', 'articles': {'passed': True}, 'errors': [],
                'list_url': item['url'], 'list_snapshot': {
                    'url': item['url'], 'hash': item['content_hash'], 'outcome': 'usable'},
                'region': region}))
        result = _native_receipt(event, source, SECOND_HASH, item['url'])
        self.assertIsNotNone(result)
        return result

    def test_only_unspecified_profiles_use_shared_audience(self):
        for value in (None, '', [], ()):
            self.assertEqual(normalize_profiles(value), ALL_PROFILES)
        self.assertEqual(normalize_profiles(['teacher', 'undergraduate', 'teacher']),
                         ('undergraduate', 'teacher'))
        for value in ('student', ['teacher', 'unknown'], {}, 1, [None]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_profiles(value)

    def test_undergraduate_graduate_and_teacher_priorities_differ(self):
        funding = page('funding', '国家奖学金申请')
        research = page('research', '科研项目申报')
        personnel = page('personnel', '人事处职称评审')
        self.assertLess(candidate_attention(funding, profiles='undergraduate')['rank'],
                        candidate_attention(personnel, profiles='undergraduate')['rank'])
        self.assertLess(candidate_attention(research, profiles='graduate')['rank'],
                        candidate_attention(research, profiles='undergraduate')['rank'])
        self.assertLess(candidate_attention(personnel, profiles='teacher')['rank'],
                        candidate_attention(funding, profiles='teacher')['rank'])

    def test_combined_profiles_and_same_round_needs_are_preserved(self):
        pages = [page('aid', '学生资助'), page('enrol', '推荐免试招生'),
                 page('staff', '教师职称'), page('faculty', '院系设置')]
        results = [candidate_attention(item, profiles=['undergraduate', 'teacher']) for item in pages]
        self.assertTrue(all(result['score'] > 0 for result in results))
        self.assertEqual({result['needs'][0] for result in results},
                         {'funding', 'admissions', 'personnel', 'academic_units'})

    def test_weights_are_supplied_and_bad_configuration_is_not_silenced(self):
        item = page('aid', '学生资助')
        self.assertEqual(candidate_attention(item, weights={'funding': 7})['score'], 7)
        for weights in ({'typo': 2}, {'funding': 0}, {'funding': float('nan')},
                        {'funding': True}, {'funding': '3'}, []):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                candidate_attention(item, weights=weights)

    def test_strange_chinese_label_uses_real_current_parent_context(self):
        parent = page('centre', '学生资助管理中心', state='fetched', content_hash=HASH)
        parent['kind'] = 'unit'
        child = page('odd', '树立和践行正确政绩观学习教育')
        edge = dict(parent_url=parent['url'], target_url=child['url'], label='专题入口',
                    decision='follow', content_hash=HASH, locator='#observed')
        hints = need_evidence(child, incoming=[edge], parents={parent['url']: parent})
        self.assertIn('funding', hints['needs'])
        proof = next(item for item in hints['evidence'] if item['category'] == 'funding')
        self.assertEqual((proof['url'], proof['locator'], proof['content_hash']),
                         (parent['url'], '#observed', HASH))
        self.assertNotIn('owner', hints)

    def test_stale_rejected_or_wrong_target_edges_cannot_supply_context(self):
        parent = page('centre', '学生资助管理中心', state='fetched', content_hash=HASH)
        parent['kind'] = 'unit'
        child = page('odd', '更多')
        base = dict(parent_url=parent['url'], target_url=child['url'], label='奖助学金',
                    decision='follow', content_hash=HASH)
        for changes in ({'content_hash': 'old'}, {'decision': 'route_reference'},
                        {'target_url': ROOT + 'different'}):
            self.assertNotIn('funding', need_evidence(child, incoming=[dict(base, **changes)],
                                                     parents={parent['url']: parent})['needs'])

    def test_parent_feed_only_contributes_the_matching_child_list(self):
        child = page('jobs', '更多')
        parent = page('home', '学校主页', state='fetched', content_hash=HASH, feed_json=json.dumps({'lists': [
            {'column_url': ROOT + 'aid', 'name': '奖学金', 'samples': [{'title': '国家奖学金名单'}]},
            {'column_url': child['url'], 'name': '就业信息', 'samples': [{'title': '校园招聘会安排'}]},
        ]}))
        parent['kind'] = 'root'
        edge = dict(parent_url=parent['url'], target_url=child['url'], label='更多',
                    decision='follow', content_hash=HASH)
        result = need_evidence(child, incoming=[edge], parents={parent['url']: parent})
        self.assertIn('career', result['needs'])
        self.assertNotIn('funding', result['needs'])

    def test_same_url_multiple_lists_require_specific_observed_locator(self):
        child = page('shared', '更多')
        parent = page('home', '学校主页', state='fetched', content_hash=HASH, feed_json={'lists': [
            {'column_url': child['url'], 'name': '奖学金', 'column_link_locator': '#aid-more'},
            {'column_url': child['url'], 'name': '人才招聘', 'column_link_locator': '#staff-more'},
        ]})
        parent['kind'] = 'root'
        edge = dict(parent_url=parent['url'], target_url=child['url'], label='更多',
                    decision='follow', content_hash=HASH, locator='#aid-more')
        result = need_evidence(child, incoming=[edge], parents={parent['url']: parent})
        self.assertEqual(result['needs'], ('funding',))
        edge['locator'] = '#unknown'
        self.assertEqual(need_evidence(child, incoming=[edge], parents={parent['url']: parent})['needs'], ())

    def test_bad_json_and_non_mapping_feed_rows_are_safe(self):
        for value in ('{bad', 'null', '42', '"x"', {'lists': [None, 8]}, {'lists': {}}):
            item = page('plain', '普通页面', feed_json=value)
            item['path_json'] = value
            self.assertEqual(need_evidence(item)['needs'], ())
        item = page('aid', '普通页面')
        item['path_json'] = '[null, 42, {"label":"科研"}, "奖学金"]'
        self.assertEqual(need_evidence(item)['needs'], ('funding',))

    def test_probing_does_not_claim_validated_source_or_reduce_semantic_score(self):
        item = page('aid', '学生资助', state='fetched', content_hash=HASH)
        coverage = coverage_state({item['url']: item})
        cell = coverage['cells'][item['url']]['funding']
        self.assertEqual(cell['validated'], [])
        self.assertEqual(len(cell['probes']), 1)
        self.assertEqual(candidate_attention(item, coverage=coverage)['rank'], candidate_attention(item)['rank'])

    def test_same_named_units_and_school_level_source_do_not_cover_each_other(self):
        first = page('a/aid', '奖学金', state='fetched', content_hash=HASH)
        second = page('b/aid', '奖学金', state='fetched', content_hash=SECOND_HASH)
        central = page('central/aid', '奖学金', state='fetched', content_hash=THIRD_HASH)
        pages = {item['url']: item for item in (first, second, central)}
        branches = {first['url']: ROOT + 'a', second['url']: ROOT + 'b', central['url']: ROOT}
        coverage = coverage_state(pages, branches=branches, validated_sources=[receipt(first)])
        self.assertEqual(coverage['cells'][ROOT + 'a']['funding']['validated'], ['1'])
        self.assertEqual(coverage['cells'][ROOT + 'b']['funding']['validated'], [])
        self.assertEqual(coverage['cells'][ROOT]['funding']['validated'], [])
        self.assertGreater(candidate_attention(first, branches=branches, coverage=coverage)['rank'],
                           candidate_attention(second, branches=branches, coverage=coverage)['rank'])

    def test_invalid_or_unconfirmed_receipts_do_not_fill_coverage(self):
        item = page('aid', '奖学金', state='fetched', content_hash=HASH)
        variants = [dict(receipt(item), content_hash='old'), dict(receipt(item), region=''),
                    dict(receipt(item), config_hash=''), dict(receipt(item), source_id=None),
                    dict(receipt(item), status='proposed')]
        for candidate in variants:
            coverage = coverage_state({item['url']: item}, validated_sources=[candidate])
            self.assertEqual(coverage['cells'][item['url']]['funding']['validated'], [])

    def test_validation_is_deduplicated_by_stable_id_and_keeps_receipt_evidence(self):
        item = page('aid', '奖学金', state='fetched', content_hash=HASH)
        proof = receipt(item)
        coverage = coverage_state({item['url']: item}, validated_sources=[proof, proof])
        cell = coverage['cells'][item['url']]['funding']
        self.assertEqual(cell['validated'], ['1'])
        self.assertEqual(cell['validation_records'], [proof])

    def test_same_page_receipt_cannot_borrow_adjacent_column_sample(self):
        item = page('shared', '信息公开', state='fetched', content_hash=HASH, feed_json={'lists': [
            {'column_url': ROOT + 'shared', 'name': '工作动态', 'container_locator': '#aid',
             'samples': [{'title': '国家奖学金名单公示'}]},
            {'column_url': ROOT + 'shared', 'name': '通知公告', 'container_locator': '#staff',
             'samples': [{'title': '教师职称评审安排'}]},
        ]})
        coverage = coverage_state({item['url']: item}, validated_sources=[receipt(item, region='#staff')])
        self.assertNotIn('funding', coverage['cells'].get(item['url'], {}))
        self.assertEqual(coverage['cells'][item['url']]['personnel']['validated'], ['1'])

    def test_selected_region_does_not_borrow_global_title_or_parent_other_module(self):
        item = page('shared', '奖学金与教师职称信息', state='fetched', content_hash=HASH, feed_json={'lists': [
            {'column_url': ROOT + 'shared', 'name': '奖学金', 'container_locator': '#aid'},
            {'column_url': ROOT + 'shared', 'name': '教师职称', 'container_locator': '#staff'},
        ]})
        parent = page('home', '首页', state='fetched', content_hash=SECOND_HASH, feed_json={'lists': [
            {'column_url': item['url'], 'name': '奖助学金', 'column_link_locator': '#aid-more'},
        ]})
        parent['kind'] = 'root'
        edge = dict(parent_url=parent['url'], target_url=item['url'], label='奖助学金',
                    decision='follow', content_hash=SECOND_HASH, locator='#aid-more')
        coverage = coverage_state({item['url']: item, parent['url']: parent}, edges=[edge],
                                  validated_sources=[receipt(item, region='#staff')])
        cells = coverage['cells'][item['url']]
        self.assertEqual(cells['funding']['validated'], [])
        self.assertEqual(cells['personnel']['validated'], ['1'])
        result = candidate_attention(item, coverage=coverage)
        self.assertEqual(result['coverage']['personnel']['state'], 'validated')
        self.assertEqual(result['coverage']['funding']['state'], 'probed')
        json.dumps(result)

    def test_unknown_region_in_multicolumn_page_does_not_claim_category(self):
        item = page('shared', '奖学金与教师职称', state='fetched', content_hash=HASH, feed_json={'lists': [
            {'column_url': ROOT + 'shared', 'name': '奖学金', 'container_locator': '#aid'},
            {'column_url': ROOT + 'shared', 'name': '教师职称', 'container_locator': '#staff'},
        ]})
        coverage = coverage_state({item['url']: item}, validated_sources=[receipt(item, region='#missing')])
        self.assertTrue(all(not cell['validated'] for cell in coverage['cells'][item['url']].values()))

    def test_network_failure_does_not_supply_negative_semantic_feedback(self):
        pending = page('aid', '奖学金', state='pending', attempts=0)
        failed = dict(pending, state='failed', attempts=99, error='network_timeout')
        coverage = coverage_state({failed['url']: failed})
        self.assertEqual(coverage['cells'][failed['url']]['funding']['probes'], [])
        self.assertEqual(candidate_attention(pending)['rank'],
                         candidate_attention(failed, coverage=coverage)['rank'])

    def test_network_failure_keeps_prior_matching_verified_material(self):
        item = page('aid', '奖学金', state='failed', content_hash=HASH, error='timeout')
        result = coverage_state({item['url']: item}, validated_sources=[receipt(item)])
        self.assertEqual(result['cells'][item['url']]['funding']['validated'], ['1'])

    def test_low_related_source_remains_a_deterministic_candidate(self):
        item = page('library', '图书馆数据检索库', student_value='low')
        result = candidate_attention(item, profiles='undergraduate')
        self.assertEqual(result['rank'], (0,))
        self.assertEqual(result['reason'], 'unclassified_or_other_interest_retained')
        self.assertNotIn('reject', result)
        self.assertEqual(item['student_value'], 'low')

    def test_teacher_personnel_and_research_are_not_student_vetoed(self):
        for label in ('人事处教师职称', '科研处基金项目申报'):
            item = page('staff', label, student_value='low')
            result = candidate_attention(item, profiles='teacher')
            self.assertGreater(result['score'], 0)
            self.assertTrue(result['evidence'])

    def test_reviewed_human_resources_major_is_not_the_personnel_office(self):
        item = page('major', '人力资源管理')
        item.update(kind='major', path_json='["院系设置", "管理学院", "专业介绍"]')
        result = candidate_attention(item, profiles='undergraduate')
        self.assertIn('academic_units', result['needs'])
        self.assertNotIn('personnel', result['needs'])
        self.assertGreater(result['score'], 0)
        office = dict(item, kind='unit')
        self.assertIn('personnel', need_evidence(office)['needs'])

    def test_category_coverage_is_not_a_whole_school_percentage(self):
        item = page('aid', '奖学金', state='fetched', content_hash=HASH)
        result = coverage_state({item['url']: item}, validated_sources=[receipt(item)])
        self.assertEqual(set(result), {'cells'})
        self.assertNotIn('percentage', result)
        self.assertEqual(set(result['cells'][item['url']]), {'funding'})
        self.assertGreater(len(CATEGORY_KEYS), len(result['cells'][item['url']]))

    def test_actual_native_receipt_region_counts_only_its_executed_name(self):
        item = page('shared', '奖学金与教师职称', state='fetched', content_hash=HASH, feed_json={'lists': [
            {'column_url': ROOT + 'shared', 'name': '奖学金', 'container_locator': '#aid ul'},
            {'column_url': ROOT + 'shared', 'name': '教师职称', 'container_locator': '#staff ul'},
        ]})
        region = {'name': '国家奖学金', 'container': '#aid', 'heading': '#aid > h2',
                  'basis': 'executed_ai_scope'}
        proof = self.native_receipt(item, region)
        coverage = coverage_state({item['url']: item}, validated_sources=[proof])
        cells = coverage['cells'][item['url']]
        self.assertEqual(cells['funding']['validated'], ['8'])
        self.assertEqual(cells['personnel']['validated'], [])
        self.assertEqual(cells['funding']['validation_records'][0]['region'], region)
        self.assertLess(candidate_attention(item)['rank'],
                        candidate_attention(item, coverage=coverage)['rank'])

    def test_native_container_binding_uses_only_exact_local_feed_samples(self):
        item = page('shared', '信息公开', state='fetched', content_hash=HASH, feed_json={'lists': [
            {'column_url': ROOT + 'shared', 'name': '通知公告', 'container_locator': '#aid',
             'samples': [{'title': '国家奖学金名单公示'}]},
            {'column_url': ROOT + 'shared', 'name': '通知公告', 'container_locator': '#staff',
             'samples': [{'title': '教师职称评审安排'}]},
        ]})
        proof = self.native_receipt(item, {'name': '通知公告', 'container': '#aid',
                                           'heading': '#aid > h2'})
        coverage = coverage_state({item['url']: item}, validated_sources=[proof])
        self.assertEqual(coverage['cells'][item['url']]['funding']['validated'], ['8'])
        self.assertNotIn('personnel', coverage['cells'][item['url']])

    def test_native_heading_locator_is_never_treated_as_semantic_title(self):
        item = page('notices', '更多', state='fetched', content_hash=HASH)
        proof = self.native_receipt(item, {'name': '通知公告', 'container': '#section',
                                           'heading': '#奖学金科研人才招聘 > h2'})
        result = coverage_state({item['url']: item}, validated_sources=[proof])
        self.assertEqual(result['cells'], {})

    def test_dynamic_shell_and_incomplete_material_do_not_count_as_successful_probes(self):
        item = page('aid', '学生资助', state='fetched', content_hash=HASH)
        for changes in ({'health': 'dynamic_content'}, {'health': 'template_error'},
                        {'health': 'needs_adapter'}, {'outcome': 'needs_adapter'},
                        {'outcome': 'requires_render'}, {'error_code': 'dynamic_material_incomplete'},
                        {'error': 'partial_material'}, {'status_code': 502},
                        {'status_code': True}, {'status_code': '200'},
                        {'content_hash': 'truncated-hash'}):
            with self.subTest(changes=changes):
                partial = dict(item, **changes)
                coverage = coverage_state({partial['url']: partial})
                self.assertEqual(coverage['cells'][partial['url']]['funding']['probes'], [])
                self.assertEqual(candidate_attention(partial, coverage=coverage)['rank'],
                                 candidate_attention(item)['rank'])
        self.assertEqual(len(coverage_state({item['url']: item})['cells'][item['url']]['funding']['probes']), 1)

    def test_dynamic_parent_does_not_supply_a_successful_navigation_context(self):
        parent = page('centre', '学生资助管理中心', state='fetched', content_hash=HASH,
                      health='dynamic_content')
        parent['kind'] = 'unit'
        child = page('odd', '更多')
        edge = dict(parent_url=parent['url'], target_url=child['url'], label='奖学金',
                    decision='follow', content_hash=HASH, locator='#observed')
        self.assertEqual(need_evidence(child, incoming=[edge], parents={parent['url']: parent})['needs'], ())


if __name__ == '__main__':
    unittest.main()
