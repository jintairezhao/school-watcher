import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.source_inventory import Inventory
from backend.services.source_baselines import save_baseline_check
from backend.scraper.discovery.inventory_crawler import crawl_site

URL = 'https://www.example.edu.cn/'
HTML = '<main><h4><a href="/eng/">工程学院</a></h4><ul><li><a href="/civil/">土木工程系</a></li></ul></main>'


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Inventory(Path(self.temp.name) / 'inventory.db')
        self.key = self.store.ensure_site('某大学', URL)
        with self.store.connect() as c:
            c.execute("UPDATE pages SET kind='directory',label='院系设置' WHERE site_key=?", (self.key,))
        crawl_site(self.store, self.key, max_pages=1,
                   fetcher=lambda url: {'html': HTML, 'url': url, 'status': 200})
        self.baseline = {'id': 'academic', 'root_url': URL, 'reference_url': URL,
                         'reference_hash': hashlib.sha256(HTML.encode()).hexdigest(),
                         'scope': '院系表', 'scope_selector': 'main', 'category': 'academic_units',
                         'reviewed_at': '2026-09-19', 'entries': [
                             {'id': 'eng', 'name': '工程学院', 'url': URL + 'eng/'},
                             {'id': 'civil', 'name': '土木工程系', 'url': URL + 'civil/', 'parent': 'eng'},
                         ]}

    def test_matched_roster_scope_does_not_claim_whole_school_coverage(self):
        result = save_baseline_check(self.store, self.baseline)
        self.assertTrue(result['scope_passed'])
        self.assertEqual(result['matched'], 2)
        report = self.store.report(self.key)
        self.assertFalse(report['accepted'])
        self.assertIsNone(report['metrics']['units']['percent'])

    def test_matching_names_and_urls_with_wrong_parent_fails(self):
        with self.store.connect() as c:
            c.execute("UPDATE structure SET parent_key='' WHERE name='土木工程系'")
        result = save_baseline_check(self.store, self.baseline)
        self.assertFalse(result['scope_passed'])
        self.assertEqual(result['entries'][1]['status'], 'wrong_parent')

    def test_matching_parent_cannot_hide_a_wrong_relationship_type(self):
        self.baseline['entries'][1]['relation'] = 'attached_unit'
        result = save_baseline_check(self.store, self.baseline)
        self.assertFalse(result['scope_passed'])
        self.assertEqual(result['entries'][1]['status'], 'wrong_relation')

    def test_incomplete_baseline_flags_extra_units(self):
        self.baseline['entries'].pop()
        result = save_baseline_check(self.store, self.baseline)
        self.assertEqual(result['matched'], 1)
        self.assertFalse(result['scope_passed'])
        self.assertEqual(result['unexpected'][0]['name'], '土木工程系')

    def test_changed_reference_invalidates_previously_matching_review(self):
        save_baseline_check(self.store, self.baseline)
        self.store.finish(self.key, URL, html=HTML + '<p>新内容</p>', state='fetched')
        self.assertFalse(self.store.report(self.key)['reference_checks'][0]['reference_current'])
        result = save_baseline_check(self.store, self.baseline)
        self.assertEqual(result['status'], 'reference_changed')
        self.assertFalse(result['scope_passed'])

    def test_failed_revisit_cannot_keep_a_scope_current_using_its_old_snapshot(self):
        save_baseline_check(self.store, self.baseline)
        self.store.finish(self.key, URL, state='failed', error='HTTP 503')
        self.assertEqual(self.store.report(self.key)['reference_checks'][0]['status'], 'reference_unavailable')
        result = save_baseline_check(self.store, self.baseline)
        self.assertEqual(result['status'], 'reference_unavailable')
        self.assertFalse(result['scope_passed'])

    def test_repeated_placement_interpretation_does_not_invalidate_unchanged_review(self):
        nodes = [dict(key=n['node_key'], name=n['name'], kind=n['kind'], url=n['url'],
                      parent=n['parent_key'], locator=n['locator'], relation=n['relation'])
                 for n in self.store.structure(self.key) if n['reference_url'] == URL]
        primary = next(n for n in nodes if n['name'] == '土木工程系')
        self.assertTrue(save_baseline_check(self.store, self.baseline)['scope_passed'])
        for _ in range(2):
            self.store.record_structure(self.key, URL, [dict(primary, relation='navigation_entry'), *nodes],
                                        self.baseline['reference_hash'])
            self.assertTrue(self.store.report(self.key)['reference_checks'][0]['scope_passed'])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM structure_history').fetchone()[0], 0)

    def test_changed_node_kind_invalidates_review_even_if_identity_stays_the_same(self):
        nodes = [dict(key=n['node_key'], name=n['name'], kind=n['kind'], url=n['url'],
                      parent=n['parent_key'], locator=n['locator'], relation=n['relation'])
                 for n in self.store.structure(self.key) if n['reference_url'] == URL]
        self.assertTrue(save_baseline_check(self.store, self.baseline)['scope_passed'])
        next(n for n in nodes if n['name'] == '土木工程系')['kind'] = 'group'
        self.store.record_structure(self.key, URL, nodes, self.baseline['reference_hash'])
        self.assertEqual(self.store.report(self.key)['reference_checks'][0]['status'], 'structure_changed')

    def test_wrong_url_cannot_pass_by_name(self):
        self.baseline['entries'][1]['url'] = URL + 'wrong/'
        result = save_baseline_check(self.store, self.baseline)
        self.assertEqual(result['entries'][1]['status'], 'wrong_url')

    def test_publication_baseline_rejects_a_selector_that_leaks_into_other_columns(self):
        import json
        from backend.scraper.discovery.structure import publication_evidence
        html = ''.join('<section id="' + name + '"><h2><a href="/' + name + '/">' + label +
                       '</a></h2><ul class="news-list">' + ''.join('<li><a href="/info/1/' + str(start + i) +
                       '.htm">新学期工作安排通知' + str(i) + '</a><time>2026-09-18</time></li>' for i in range(3)) + '</ul></section>'
                       for name, label, start in [('notices', '通知公告', 10000), ('events', '学术活动', 20000)])
        evidence = publication_evidence(html, URL)
        self.store.finish(self.key, URL, html=html, state='fetched', feed_json=json.dumps(evidence))
        baseline = dict(self.baseline, category='publication_columns', scope_selector='section', reference_hash=hashlib.sha256(html.encode()).hexdigest(),
                        entries=[{'id': name, 'name': label, 'url': URL + name + '/', 'scope_selector': '#' + name,
                                  'article_urls': [URL + 'info/1/' + str(start + i) + '.htm' for i in range(3)]}
                                 for name, label, start in [('notices', '通知公告', 10000), ('events', '学术活动', 20000)]])
        self.assertTrue(save_baseline_check(self.store, baseline)['scope_passed'])
        partial = dict(baseline, scope_selector='#notices', entries=baseline['entries'][:1])
        self.assertTrue(save_baseline_check(self.store, partial)['scope_passed'])
        evidence['lists'][0]['list_selector'] = '.news-list li'
        self.store.finish(self.key, URL, html=html, state='fetched', feed_json=json.dumps(evidence))
        self.assertEqual(self.store.report(self.key)['reference_checks'][0]['status'], 'publication_changed')
        self.assertFalse(save_baseline_check(self.store, partial)['scope_passed'])
        result = save_baseline_check(self.store, baseline)
        self.assertFalse(result['scope_passed'])
        self.assertEqual(result['matched'], 1)
        from scripts.verify_source_inventory import verify_file
        path = Path(self.temp.name) / 'columns.json'
        path.write_text(json.dumps(baseline), encoding='utf-8')
        self.store.finish(self.key, URL, state='fetched', fetched_at='2026-09-01T00:00:00+00:00')
        self.assertTrue(verify_file(self.store, path, reparse=True)['scope_passed'])
        current = next(p for p in self.store.report(self.key)['pages'] if p['url'] == URL)
        self.assertEqual(current['checked_at'], '2026-09-01T00:00:00+00:00')


if __name__ == '__main__':
    unittest.main()
