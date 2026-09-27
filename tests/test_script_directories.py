"""Literal website data preserves hierarchy without executing page scripts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.literal_data import literal_assignment
from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_inventory import Inventory

ROOT = 'https://www.zju.edu.cn/'
URL = ROOT + 'xywxw/list.htm'


def script(binding, data):
    return '<script>' + binding + '=' + json.dumps(data, ensure_ascii=False) + ';</script>'


class LiteralDirectoryTests(unittest.TestCase):
    def test_quoted_code_stays_text_and_escapes_decode(self):
        result = literal_assignment(r'''/* intro */ list._college = [
            {title:'\u5b66\u9662', link:'https://a.zju.edu.cn/',
             url:'', note:"doThing(); // text", quote:'\'\x41', emoji:'\ud83d\ude00',},
        ]; // end''', 'list._college')
        self.assertEqual(result[0]['title'], '学院')
        self.assertEqual(result[0]['note'], 'doThing(); // text')
        self.assertEqual(result[0]['quote'], "'A")
        self.assertEqual(result[0]['emoji'], '😀')

    def test_calls_expressions_interpolation_duplicates_and_trailing_code_rejected(self):
        for value in ('getDirectory()', '[...other]', '[{title: name}]', '[`学院${run()}`]',
                      '[{title:"A",title:"B"}]', '[{title:"A"+"B"}]',
                      '[{__proto__:{}}]', '["\\q"]', '["\\ud800"]',
                      '[]; sendData()', '[]; list._college.push({})',
                      '[' * 30 + '"x"' + ']' * 30, '[,"x"]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                literal_assignment('list._college=' + value, 'list._college')

    def test_units_keep_websites_nested_parent_and_shared_introduction(self):
        data = [{'title': '信息学部', 'link': '/xxxb/list.htm', 'url': '', 'children': [
            {'title': '计算机科学与技术学院', 'link': 'http://www.cs.zju.edu.cn/',
             'url': '/intro/page.htm', 'children': [
                 {'title': '网络空间安全学院', 'link': 'https://icsr.zju.edu.cn/', 'url': '/intro/page.htm'}]}]}]
        parsed = extract_structure(script('list._college', data), URL, ROOT, 'directory', '学院（系）')
        nodes = {n['name']: n for n in parsed['nodes']}
        self.assertEqual(nodes['信息学部']['kind'], 'group')
        self.assertEqual(nodes['计算机科学与技术学院']['parent'], nodes['信息学部']['key'])
        child = nodes['网络空间安全学院']
        self.assertEqual(child['parent'], nodes['计算机科学与技术学院']['key'])
        self.assertEqual(child['relation'], 'nested_directory_entry')
        self.assertEqual(child['url'], 'https://icsr.zju.edu.cn/')
        self.assertEqual(len([n for n in parsed['nodes'] if n['kind'] == 'unit']), 2)
        introductions = [l for l in parsed['links'] if l['url'] == ROOT + 'intro/page.htm']
        self.assertEqual(len(introductions), 2)
        self.assertTrue(all(l['kind'] == 'navigation' for l in introductions))
        self.assertIn('list._college[0].children[0].children[0]', child['locator'])

    def test_repeated_menu_positions_do_not_form_cycles(self):
        data = [{'name': '学校机构', 'href': '/xxjg/list.htm', 'children': [
            {'name': '学校机构', 'href': '/xxjg/list.htm', 'children': [
                {'name': '行政机构', 'href': '/xzjg/list.htm'}]},
            {'name': '学院（系）', 'href': '/xywxw/list.htm'}]}]
        parsed = extract_structure(script('main._menu', data), ROOT, ROOT, 'root', '浙江大学')
        nodes = parsed['nodes']
        self.assertEqual(len({n['key'] for n in nodes}), 5)
        self.assertTrue(all(n['key'] != n['parent'] for n in nodes))
        self.assertEqual([l['kind'] for l in parsed['links'] if l['label'] == '学院（系）'], ['directory'])
        parents = {n['key']: n['parent'] for n in nodes}
        for key in parents:
            visited = set()
            while key:
                self.assertNotIn(key, visited)
                visited.add(key)
                key = parents[key]

    def test_private_and_service_links_are_retained_but_not_enqueued(self):
        data = [{'name': '学校机构', 'href': '/xxjg/list.htm', 'children': [
            {'name': '规范性文件', 'href': 'http://10.203.3.207:8080/xwfw/'},
            {'name': '办公系统', 'href': 'https://portal.zju.edu.cn/'},
            {'name': '学院（系）', 'href': '/xywxw/list.htm'}]}]
        html = script('main._menu', data)
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder) / 'inventory.db')
            key = inventory.ensure_site('浙江大学', ROOT)
            inventory.enqueue(key, ROOT, '浙江大学', 'root', 0, [], 'school_domain')
            report = inventory.report(key)
            inspect_page(inventory, report['site'], report['pages'][0],
                         fetcher=lambda _: {'url': ROOT, 'status': 200, 'html': html})
            urls = {p['url'] for p in inventory.report(key)['pages']}
            self.assertIn(URL, urls)
            self.assertNotIn('http://10.203.3.207:8080/xwfw/', urls)
            self.assertNotIn('https://portal.zju.edu.cn/', urls)
            retained = [n for n in inventory.structure(key) if n['name'] == '规范性文件']
            self.assertEqual(retained[0]['url'], 'http://10.203.3.207:8080/xwfw/')

    def test_incomplete_or_conflicting_binding_is_atomic_and_requires_review(self):
        good = {'title': '文学院', 'link': 'https://www.lit.zju.edu.cn/', 'url': ''}
        for html in (script('list._college', [good, {'title': '', 'children': [good]}]),
                     script('list._college', [good]) * 2,
                     '<script>list._college=[]; run();</script>'):
            with self.subTest(html=html):
                parsed = extract_structure(html, URL, ROOT, 'directory', '学院（系）')
                self.assertEqual(len(parsed['nodes']), 1)
                self.assertTrue(any(n.startswith('literal_directory_requires_review:') for n in parsed['notes']))

    def test_other_sites_and_repeated_college_data_on_other_pages_are_not_rosters(self):
        html = script('list._college', [{'title': '医学院', 'link': 'https://www.cmm.zju.edu.cn/', 'url': ''}])
        for url in ('https://other.edu.cn/xywxw/list.htm', ROOT + 'rwxb_76786/list.htm'):
            parsed = extract_structure(html, url, ROOT, 'directory', '目录')
            self.assertEqual(len(parsed['nodes']), 1)


if __name__ == '__main__':
    unittest.main()
