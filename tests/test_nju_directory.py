"""NJU's exact directory groups and expandable lists preserve unit identities."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_inventory import Inventory
from backend.services.source_relationships import SourceRelationships

ROOT = 'https://www.nju.edu.cn/'
URL = ROOT + 'xybm.htm'


def block(category, rows):
    return '<div class="yxbm"><ul><h3 class="wl">' + category + '</h3>' + rows + '</ul></div>'


class NjuDirectoryTests(unittest.TestCase):
    def parse(self, html, url=URL):
        return extract_structure(html, url, ROOT, 'directory', '学院部门')

    def test_category_is_not_a_college_and_missing_links_remain_missing(self):
        html = block('学院', '<li><h4 class="wl"><a>药学院</a></h4></li>'
                     '<li><h4 class="wl"><a href="http://nh.nju.edu.cn">'
                     '南京赫尔辛基大气与地球系统科学学院<br>（南赫学院）</a></h4></li>')
        result = self.parse(html)
        nodes = {n['name']: n for n in result['nodes']}
        self.assertEqual(nodes['学院']['kind'], 'group')
        self.assertEqual(nodes['学院']['relation'], 'directory_group')
        self.assertEqual(nodes['药学院']['parent'], nodes['学院']['key'])
        self.assertEqual(nodes['药学院']['url'], '')
        self.assertEqual(nodes['南京赫尔辛基大气与地球系统科学学院 （南赫学院）']['url'], 'http://nh.nju.edu.cn/')
        self.assertEqual(next(l for l in result['links'] if l['label'] == '药学院')['decision'], 'missing_link')

    def test_expandable_colleges_and_offices_keep_each_immediate_parent(self):
        html = block('学院', '<li><h4 class="wl"><a href="https://nubs.nju.edu.cn/">商学院</a><span>展开</span></h4>'
                     '<div class="bm-er"><a href="http://njubs.nju.edu.cn/intro.php/a">经济学院</a>'
                     '<a href="http://njubs.nju.edu.cn/intro.php/e">管理学院</a></div></li>')
        html += block('行政部门', '<li><h4 class="wl"><a>本科生院</a><span>展开</span></h4>'
                      '<div class="bm-er"><a href="https://jw.nju.edu.cn/main.htm">教学运行服务中心</a>'
                      '<a href="https://jw.nju.edu.cn/main.htm">综合办公室</a><a>学生发展支持中心</a></div></li>'
                      '<li><h4 class="wl"><a href="http://job.nju.edu.cn/#!/home">学生就业指导中心</a></h4></li>')
        nodes = {n['name']: n for n in self.parse(html)['nodes']}
        for name, parent in [('经济学院', '商学院'), ('管理学院', '商学院'),
                             ('教学运行服务中心', '本科生院'), ('综合办公室', '本科生院'), ('学生发展支持中心', '本科生院')]:
            self.assertEqual(nodes[name]['parent'], nodes[parent]['key'])
            self.assertEqual(nodes[name]['relation'], 'nested_directory_entry')
        self.assertNotEqual(nodes['教学运行服务中心']['key'], nodes['综合办公室']['key'])
        self.assertEqual(nodes['学生就业指导中心']['url'], 'http://job.nju.edu.cn/#!/home')
        self.assertEqual(nodes['学生就业指导中心']['parent'], nodes['行政部门']['key'])

    def test_shared_office_homepage_does_not_make_all_three_names_publishers(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder) / 'inventory.db')
            key = inventory.ensure_site('南京大学', ROOT)
            unit, channel = 'https://ndbgs.nju.edu.cn/', 'https://ndbgs.nju.edu.cn/notices/'
            rows = ''.join('<li><h4 class="wl"><a href="' + unit + '">' + name + '</a></h4></li>'
                           for name in ('南京大学办公室', '保密办公室', '法制办公室'))
            for url, kind, label, html in [
                (URL, 'directory', '学院部门', block('党群组织', rows)),
                (unit, 'unit', '南京大学办公室', '<title>南京大学办公室</title><nav><a href="' + channel + '">通知公告</a></nav>'),
            ]:
                inventory.enqueue(key, url, label, kind, 1, [], 'school_domain')
                report = inventory.report(key)
                page = next(p for p in report['pages'] if p['url'] == url)
                inspect_page(inventory, report['site'], page,
                             fetcher=lambda _: {'url': url, 'status': 200, 'html': html})
            records = inventory.structure(key)
            entries = [n for n in records if n['reference_url'] == URL and n['url'] == unit]
            self.assertEqual(len(entries), 3)
            paths = SourceRelationships(inventory.report(key), records).paths_for(channel)
            self.assertEqual([p['unit_name'] for p in paths], ['南京大学办公室'])
            self.assertEqual(paths[0]['nodes'][-2]['name'], '党群组织')
            self.assertFalse(paths[0]['verified'])

    def test_combined_labels_remain_one_entry_and_adapter_is_page_specific(self):
        html = block('直属单位', '<li><h4 class="wl"><a href="http://dawww.nju.edu.cn/">档案馆 校史研究室</a></h4>'
                     '<div class="bm-er"><a href="https://rabe.nju.edu.cn/">拉贝与国际安全区纪念馆</a></div></li>')
        nodes = {n['name']: n for n in self.parse(html)['nodes']}
        self.assertEqual(nodes['档案馆 校史研究室']['url'], 'http://dawww.nju.edu.cn/')
        self.assertNotIn('校史研究室', nodes)
        self.assertEqual(nodes['拉贝与国际安全区纪念馆']['parent'], nodes['档案馆 校史研究室']['key'])
        self.assertFalse(any(n['relation'] == 'directory_group' for n in self.parse(html, ROOT + 'other.htm')['nodes']))


if __name__ == '__main__':
    unittest.main()
