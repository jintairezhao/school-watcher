"""Directory position must not manufacture a parent, active unit or alias."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_inventory import Inventory
from backend.services.source_relationships import SourceRelationships

ROOT = 'https://www.buaa.edu.cn/'
URL = ROOT + 'jgsz/jxkyjg02.htm'


def directory(rows):
    return '<div class="kyjg"><div class="kyjg-box"><div class="kyjg-tit">' \
           '<h3>融合创新示范区</h3></div><div class="kyjg-bd"><ul>' + rows + '</ul></div></div></div>'


class BuaaDirectoryTests(unittest.TestCase):
    def parse(self, rows):
        return extract_structure(directory(rows), URL, ROOT, 'directory', '教学科研机构')

    def test_parallel_colleges_are_siblings_even_when_second_has_no_usable_link(self):
        result = self.parse('<li><a href="http://ecpkn.buaa.edu.cn/">中法工程师学院</a>'
                            '<span> / </span><a href="javascript:void(0)">国际通用工程学院</a></li>')
        nodes = {n['name']: n for n in result['nodes']}
        first, second = nodes['中法工程师学院'], nodes['国际通用工程学院']
        shared = nodes['中法工程师学院 / 国际通用工程学院']
        self.assertEqual(first['parent'], shared['key'])
        self.assertEqual(second['parent'], shared['key'])
        self.assertEqual(second['relation'], 'same_directory_row')
        self.assertEqual(second['url'], '')
        self.assertEqual(shared['kind'], 'group')
        self.assertEqual(shared['parent'], nodes['融合创新示范区']['key'])
        links = [l for l in result['links'] if l['label'] == '国际通用工程学院']
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['decision'], 'missing_link')

    def test_combined_label_is_not_split_and_secondary_anchors_keep_their_positions(self):
        combined = '高等理工学院/未来空天技术学院/国家卓越工程师学院/量子科技学院'
        result = self.parse('<li><a href="https://hc.buaa.edu.cn/">沈元学院</a>'
                            '<a class="xbt" href="https://hc.buaa.edu.cn/">' + combined + '</a></li>'
                            '<li><a href="https://h3i.buaa.edu.cn/index.htm">国际创新学院</a>'
                            '<div class="xbt"><a href="https://zfai.buaa.edu.cn/">中法航空学院</a>'
                            '<span>/</span><a>中法未来科技学院</a></div></li>')
        nodes = {n['name']: n for n in result['nodes']}
        self.assertEqual(nodes[combined]['kind'], 'group')
        self.assertEqual(nodes[combined]['relation'], 'shared_directory_label')
        self.assertEqual(nodes[combined]['parent'], nodes['沈元学院']['key'])
        self.assertNotIn('量子科技学院', nodes)
        for label in ('中法航空学院', '中法未来科技学院'):
            self.assertEqual(nodes[label]['parent'], nodes['国际创新学院']['key'])
            self.assertEqual(nodes[label]['relation'], 'directory_companion_entry')
        self.assertEqual(nodes['中法未来科技学院']['url'], '')

    def test_hidden_template_entries_cannot_establish_a_publication_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder) / 'inventory.db')
            key = inventory.ensure_site('北京航空航天大学', ROOT)
            unit, notices = 'https://icat.buaa.edu.cn/', 'https://icat.buaa.edu.cn/notices/'

            def save(url, kind, label, html):
                inventory.enqueue(key, url, label, kind, 1, [], 'school_domain')
                report = inventory.report(key)
                page = next(p for p in report['pages'] if p['url'] == url)
                inspect_page(inventory, report['site'], page,
                             fetcher=lambda _: {'url': url, 'status': 200, 'html': html})

            rows = '<li><a href="http://www.ase.buaa.edu.cn/">航空科学与工程学院</a>' \
                   '<a class="xbt" style="DISPLAY: none !important" href="' + unit + \
                   '">中国商飞-北航大飞机研究院</a></li>'
            save(URL, 'directory', '教学科研机构', directory(rows))
            save(unit, 'unit', '中国商飞-北航大飞机研究院',
                 '<title>中国商飞-北航大飞机研究院</title><nav><a href="' + notices + '">通知公告</a></nav>')
            records = inventory.structure(key)
            hidden = [n for n in records if n['relation'] == 'hidden_directory_entry']
            self.assertEqual(len(hidden), 1)
            self.assertEqual(hidden[0]['url'], unit)
            self.assertEqual(SourceRelationships(inventory.report(key), records).paths_for(notices), [])
            # The same observed entry, explicitly visible, can provide a website
            # path while still making no administrative/publisher verification.
            save(URL, 'directory', '教学科研机构', directory(rows.replace('DISPLAY: none !important', 'color:blue')))
            paths = SourceRelationships(inventory.report(key), inventory.structure(key)).paths_for(notices)
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0]['nodes'][-1]['relation'], 'directory_companion_entry')
            self.assertFalse(paths[0]['verified'])

    def test_hidden_container_is_preserved_and_adapter_is_scoped_to_official_page(self):
        rows = '<li><a>国际创新学院</a><div class="xbt" hidden><a>中西智能学院</a></div></li>'
        parsed = self.parse(rows)
        child = next(n for n in parsed['nodes'] if n['name'] == '中西智能学院')
        self.assertEqual(child['relation'], 'hidden_directory_entry')
        other = extract_structure(directory(rows), ROOT + 'other.htm', ROOT, 'directory', '其他目录')
        self.assertFalse(any(n['relation'] == 'academic_group' for n in other['nodes']))


if __name__ == '__main__':
    unittest.main()
