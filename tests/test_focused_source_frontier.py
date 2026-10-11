"""Observed old listings may lead to new publishing routes without being erased."""
import json
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import crawl_site

ROOT = 'https://example.edu.cn/'
OLD, TOPIC, NEW = ROOT + 'old/notices.htm', ROOT + 'topics/', ROOT + 'current/notices.htm'


def listing(year, nearby=True):
    return ('<title>通知公告 - 测试大学经济学院</title><main><section id="notices"><h2>通知公告</h2><ul>' +
            ''.join(f'<li><a href="/info/100/{i}.htm">关于开展{year}年教学安排{i}的通知</a>'
                    f'<span>{year}-10-01</span></li>' for i in range(3)) + '</ul></section>' +
            ('<aside><h2>相关专题</h2><a href="/topics/">专题汇总</a></aside>' if nearby else '') +
            '</main><footer><a href="/footer-news/">通知公告</a></footer>')


class FocusedSourceFrontierTests(unittest.TestCase):
    def setUp(self):
        self.today = patch('backend.scraper.discovery.frontier._today', return_value=date(2026, 10, 11))
        self.today.start()
        self.addCleanup(self.today.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='focused-source-frontier-')
        self.path = Path(self.temp.name) / 'frontier.db'
        self.store = Inventory(self.path)
        self.key = self.store.ensure_site('测试大学', ROOT)
        self.store.finish(self.key, ROOT, state='reference_only')

    def tearDown(self):
        self.temp.cleanup()

    def test_old_notices_probe_a_local_topic_and_reach_a_new_list_without_dropping_old(self):
        html = {OLD: listing(2024), TOPIC: '<title>专题汇总</title><nav><a href="/current/notices.htm">最新公告</a></nav>',
                NEW: listing(2026, nearby=False)}
        self.store.enqueue(self.key, OLD, '通知公告', 'channel', 1, [], 'school_domain')
        reads = []
        def fetch(url):
            reads.append(url)
            return {'url': url, 'html': html[url], 'status': 200}
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Probe must not buy a model call')):
            crawl_site(self.store, self.key, max_pages=3, workers=1, focus='valuable', fetcher=fetch)
        self.assertEqual(reads, [OLD, TOPIC, NEW])
        self.assertEqual(self.store.get_page(self.key, NEW)['state'], 'fetched')
        self.assertEqual(self.store.get_page(self.key, OLD)['state'], 'fetched')
        self.assertEqual(json.loads(self.store.get_page(self.key, OLD)['feed_json'])['item_count'], 3)
        self.assertIsNone(self.store.get_page(self.key, ROOT + 'footer-news/'))
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT kind FROM edges WHERE parent_url=? AND target_url=?',
                                               (OLD, TOPIC)).fetchone()[0], 'navigation')

    def test_recent_list_keeps_ordinary_terminal_behavior(self):
        self.store.enqueue(self.key, OLD, '通知公告', 'channel', 1, [], 'school_domain')
        reads = []
        crawl_site(self.store, self.key, max_pages=2, workers=1, focus='valuable',
            fetcher=lambda url: reads.append(url) or {'url': url, 'html': listing(2026), 'status': 200})
        self.assertEqual(reads, [OLD])
        self.assertIsNone(self.store.get_page(self.key, TOPIC))

    def test_every_fifth_claim_reads_oldest_unread_entrance_across_process_restarts(self):
        self.store.enqueue(self.key, TOPIC, '真实入口', 'navigation', 1, [], 'school_domain')
        for index in range(8):
            self.store.enqueue(self.key, ROOT + f'notice/{index}/', '通知公告', 'channel', 1, [], 'school_domain')
        for _ in range(4):
            page = self.store.claim(self.key, focus='valuable')
            self.assertNotEqual(page['url'], TOPIC)
            self.store.finish(self.key, page['url'], state='fetched')
        restored = Inventory(self.path)
        self.assertEqual(restored.claim(self.key, focus='valuable')['url'], TOPIC)

    def test_transport_failure_does_not_change_semantic_route_rank(self):
        from backend.scraper.discovery.frontier import route_rank
        route = {'kind': 'channel', 'url': NEW, 'label': '通知公告', 'path_json': '[]',
                 'depth': 2, 'priority': 3, 'attempts': 0, 'frontier_rowid': 1}
        failed = dict(route, health='unreachable', error='HTTP 502', network_failures=3)
        self.assertEqual(route_rank(route), route_rank(failed))

    def test_copyright_or_page_level_date_does_not_trigger_old_publication_probe(self):
        from backend.scraper.discovery.frontier import old_publications
        feed = {'latest_publication': '2024-10-01', 'publication_dates_complete': False,
                'dated_item_count': 0, 'samples': [{'title': '真实通知', 'date': None}]}
        self.assertFalse(old_publications(feed))

    def test_low_value_route_is_retained_and_eventually_read(self):
        low = ROOT + 'low-notices/'
        self.store.enqueue(self.key, low, '一般通知', 'channel', 1, [], 'school_domain')
        with self.store.connect() as connection:
            connection.execute('UPDATE pages SET priority=6 WHERE url=?', (low,))
        for index in range(7):
            self.store.enqueue(self.key, ROOT + f'priority/{index}/', '学生通知', 'channel', 1, [], 'school_domain')
        seen = []
        while (page := self.store.claim(self.key, focus='valuable')) is not None:
            seen.append(page['url'])
            self.store.finish(self.key, page['url'], state='fetched')
        self.assertEqual(len(seen), 8)
        self.assertIn(low, seen)

    def parsed_routes(self, html, *, kind='channel', url=OLD, final=None, feed=None):
        from backend.scraper.discovery.structure import extract_structure
        from backend.scraper.discovery.layered import route_structure
        parsed = extract_structure(html, final or url, ROOT, kind, '真实页面', [])
        page = {'url': url, 'kind': kind, 'label': '真实页面', 'path_json': '[]'}
        return route_structure(parsed, page, html, ROOT, kind == 'channel',
                               policy='valuable', publication=feed or {
                                   'samples': [{'date': '2024-10-01'}]})

    def test_relative_topic_is_resolved_from_actual_final_page_and_html_base(self):
        for base, expected in [('', ROOT + 'renewed/topics/'),
                               ('<base href="/current/">', ROOT + 'current/topics/')]:
            with self.subTest(base=base):
                html = base + '<title>真实页面</title><aside><a href="topics/">专题汇总</a></aside>'
                links = self.parsed_routes(html, final=ROOT + 'renewed/list.htm')['links']
                followed = [link['url'] for link in links if link['decision'] == 'follow']
                self.assertEqual(followed, [expected])

    def test_old_list_keeps_observed_image_or_homepage_routes_without_invented_names(self):
        for element, expected in [('<img alt="学期专题" src="/banner.png">', '学期专题'),
                                  ('<img title="学期专题" src="/banner.png">', '学期专题'),
                                  ('<img src="/banner.png">', ''), ('首页', '首页')]:
            with self.subTest(element=element):
                html = '<title>真实页面</title><main><div class="feature-card"><a href="/topics/">' + element + '</a></div></main>'
                links = self.parsed_routes(html)['links']
                probes = [link for link in links if link.get('probe_only')]
                self.assertEqual([(link['url'], link['label']) for link in probes], [(TOPIC, expected)])
                self.assertFalse(any(node['url'] == TOPIC for node in self.parsed_routes(html)['nodes']))

    def test_homepage_unknown_image_entry_is_read_without_model_or_owner_guess(self):
        for element, expected in [('<img alt="学期专题" src="/banner.png">', '学期专题'),
                                  ('<img src="/banner.png">', '')]:
            with self.subTest(element=element):
                html = '<title>真实页面</title><main><a href="/topics/">' + element + '</a></main>'
                routes = self.parsed_routes(html, kind='root', url=ROOT)
                probes = [link for link in routes['links'] if link.get('probe_only')]
                self.assertEqual([(link['url'], link['label']) for link in probes], [(TOPIC, expected)])
                self.assertFalse(any(node['url'] == TOPIC for node in routes['nodes']))

    def test_known_opaque_articles_attachments_and_login_are_not_topic_probes(self):
        article = ROOT + 'opaque-old-target'
        html = ('<title>真实页面</title><aside><a href="/opaque-old-target">旧信息入口</a>'
                '<a href="/attachment.pdf">专题附件</a>'
                '<a href="/cas/login?service=https%3A%2F%2Fexample.edu.cn"><img src="/banner.png"></a>'
                '<a href="/topics/">专题汇总</a></aside>')
        routes = self.parsed_routes(html, feed={'samples': [{'date': '2024-10-01', 'url': article}]})
        self.assertEqual([link['url'] for link in routes['links'] if link.get('probe_only')], [TOPIC])

    def test_exploration_turn_does_not_starve_an_older_retried_route_for_new_unread_routes(self):
        from backend.scraper.discovery.frontier import choose_route
        older = {'url': TOPIC, 'kind': 'navigation', 'frontier_rowid': 2, 'attempts': 1}
        new = {'url': NEW, 'kind': 'channel', 'frontier_rowid': 3, 'attempts': 0}
        self.assertEqual(choose_route([older, new], claim_count=4), older)

    def test_only_current_followed_edges_supply_neighbor_priority(self):
        from backend.scraper.discovery.frontier import route_rank
        route = {'url': TOPIC, 'kind': 'navigation', 'label': '专题汇总'}
        parent = {'url': OLD, 'state': 'fetched', 'content_hash': 'current',
                  'feed_json': json.dumps({'samples': [{'date': '2024-10-01'}]})}
        reference = {'parent_url': OLD, 'target_url': TOPIC, 'content_hash': 'current',
                     'decision': 'route_reference'}
        followed = dict(reference, decision='follow')
        self.assertEqual(route_rank(route, [reference], {OLD: parent}), route_rank(route))
        self.assertLess(route_rank(route, [followed], {OLD: parent}), route_rank(route))

    def test_observed_office_service_icons_do_not_become_new_probes(self):
        html = ('<title>真实页面</title><main><a href="/office/oauth-redirec">'
                '<img src="/icon.png"><p>校园e办</p></a>'
                '<a href="/service/index"><img src="/icon.png"><p>网上办事</p></a>'
                '<a href="/topics/"><img src="/topic.png"></a></main>')
        routes = self.parsed_routes(html, kind='root', url=ROOT)
        self.assertEqual([link['url'] for link in routes['links'] if link.get('probe_only')], [TOPIC])


if __name__ == '__main__':
    unittest.main()
