"""A named introduction and its shared site navigation do not prove publishing ownership."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_inventory import Inventory
from backend.services.source_relationships import SourceRelationships
from backend.routes.source_structure import page_health_label, evidence_tree

ROOT = 'https://www.nju.edu.cn/'
PROFILE = 'https://nubs.nju.edu.cn/njdxjjxy/list.htm'
NEWS = 'https://nubs.nju.edu.cn/8876/list.htm'


def introduction(body='内容正在更新……'):
    return '<title>经济学院</title><nav><a href="' + NEWS + '">新闻动态</a></nav>' \
           '<h3 class="col_name"><span class="Column_Anchor">学院一览</span></h3>' \
           '<div class="col_title"><h2>经济学院</h2></div><div class="col_path">' \
           '<a href="/8877/list.htm">学院一览</a></div><div class="col_list"><ul class="wp_listcolumn">' \
           '<li class="wp_column column-6"><a class="selected" href="/njdxjjxy/list.htm">经济学院</a>' \
           '<ul class="wp_subcolumn"><li><a href="/jjxx/list.htm">经济学系</a></li>' \
           '<li><a href="/rkyjs/list.htm">人口研究所</a></li></ul></li></ul></div>' \
           '<div class="wp_articlecontent">' + body + '</div>'


class UnitProfileTests(unittest.TestCase):
    def parse(self, html):
        return extract_structure(html, PROFILE, ROOT, 'unit', '经济学院')

    def test_introduction_sidebar_keeps_units_and_never_points_a_node_to_itself(self):
        parsed = self.parse(introduction())
        entries = {n['name']: n for n in parsed['nodes'] if n['relation'] == 'unit_profile_entry'}
        self.assertEqual(set(entries), {'经济学院', '经济学系', '人口研究所'})
        self.assertEqual(entries['经济学系']['parent'], entries['经济学院']['key'])
        self.assertEqual(entries['人口研究所']['parent'], entries['经济学院']['key'])
        self.assertFalse(any(n['key'] == n['parent'] for n in parsed['nodes']))
        owner = next(n for n in parsed['nodes'] if n['relation'] == 'page_identity')
        self.assertEqual(owner['kind'], 'group')
        self.assertEqual(entries['经济学院']['parent'], owner['key'])
        self.assertEqual(next(n for n in parsed['nodes'] if n['name'] == '新闻动态')['relation'], 'shared_site_navigation')

    def test_official_roster_link_and_matching_title_do_not_turn_profile_into_a_publisher(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder) / 'inventory.db')
            key = inventory.ensure_site('南京大学', ROOT)
            roster = '<div class="yxbm"><ul><h3 class="wl">学院</h3><li><h4 class="wl">' \
                     '<a href="' + PROFILE + '">经济学院</a></h4></li></ul></div>'
            for url, label, kind, html in [(ROOT + 'xybm.htm', '学院部门', 'directory', roster),
                                           (PROFILE, '经济学院', 'unit', introduction())]:
                inventory.enqueue(key, url, label, kind, 1, [], 'school_domain')
                report = inventory.report(key)
                page = next(p for p in report['pages'] if p['url'] == url)
                inspect_page(inventory, report['site'], page,
                             fetcher=lambda _: {'url': url, 'status': 200, 'html': html})
            report = inventory.report(key)
            paths = SourceRelationships(report, inventory.structure(key))
            self.assertEqual(paths.paths_for(NEWS), [])
            page = next(p for p in report['pages'] if p['url'] == PROFILE)
            self.assertEqual(page_health_label(page), '介绍正文待更新')
            self.assertFalse(page['feed_json'])

    def test_body_present_is_a_profile_not_a_pending_or_abandoned_unit(self):
        parsed = self.parse(introduction('<p>经济学院设有多个教学科研单位。</p>'))
        self.assertTrue(any(n.startswith('unit_profile_page:') for n in parsed['notes']))
        self.assertNotIn('unit_profile_content_pending', parsed['notes'])
        page = {'state': 'fetched', 'notes_json': json.dumps(parsed['notes']), 'health': 'date_unknown'}
        self.assertEqual(page_health_label(page), '单位介绍页，发布栏目待核实')
        page.update(state='failed', health='unreachable')
        self.assertEqual(page_health_label(page), '暂时无法访问')

    def test_title_or_sidebar_alone_cannot_establish_the_profile_role(self):
        for html in [introduction().replace('学院一览', '新闻动态'),
                     introduction().replace('href="/njdxjjxy/list.htm"', 'href="/other/list.htm"'),
                     introduction().replace('class="wp_articlecontent"', 'class="other-body"'),
                     introduction().replace('<h2>经济学院</h2>', '<h2>其他学院</h2>')]:
            with self.subTest(html=html[:60]):
                self.assertFalse(any(n.startswith('unit_profile_page:') for n in self.parse(html)['notes']))

    def test_generic_self_linking_menu_does_not_create_a_self_parent(self):
        url = 'https://department.example.edu.cn/'
        html = '<nav><ul><li><a href="' + url + '">工程学院</a>' \
               '<ul><li><a href="/civil/">土木工程系</a></li></ul></li></ul></nav>'
        result = extract_structure(html, url, 'https://www.example.edu.cn/', 'unit', '工程学院')
        self.assertFalse(any(n['key'] == n['parent'] for n in result['nodes']))
        owner = next(n for n in result['nodes'] if n['relation'] == 'page_identity')
        child = next(n for n in result['nodes'] if n['name'] == '土木工程系')
        self.assertEqual(child['parent'], owner['key'])

    def test_final_address_uses_observed_redirect_without_hiding_a_newer_failure(self):
        node = {'node_key': 'profile', 'parent_key': '', 'name': '经济学院', 'url': PROFILE,
                'kind': 'unit', 'relation': 'unit_profile_entry'}
        notes = json.dumps(self.parse(introduction())['notes'])
        origin = PROFILE.replace('https:', 'http:')
        visited = {'url': origin, 'final_url': PROFILE, 'state': 'fetched', 'health': 'date_unknown',
                   'checked_at': '2026-09-21T05:41:00+00:00', 'notes_json': notes}
        pages = {origin: visited, PROFILE: {'url': PROFILE, 'state': 'pending', 'health': 'unverified'}}
        self.assertEqual(evidence_tree([node], pages)[0]['health_label'], '介绍正文待更新')
        pages[PROFILE].update(state='failed', health='unreachable', checked_at='2026-09-21T06:00:00+00:00')
        self.assertEqual(evidence_tree([node], pages)[0]['health_label'], '暂时无法访问')
        self.assertEqual(pages[origin], visited)

    def test_dynamic_shell_does_not_establish_a_final_address_visit(self):
        node = {'node_key': 'homepage', 'parent_key': '', 'name': '首页', 'url': PROFILE,
                'kind': 'unit', 'relation': 'directory_entry'}
        route = PROFILE + '#/notices'
        pages = {route: {'url': route, 'final_url': PROFILE, 'state': 'fetched', 'health': 'dynamic_content',
                         'checked_at': '2026-09-21T05:41:00+00:00'}}
        self.assertEqual(evidence_tree([node], pages)[0]['health_label'], '尚未检查')


if __name__ == '__main__':
    unittest.main()
