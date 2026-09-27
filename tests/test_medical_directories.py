"""Medical units, directory headings, office wording and conflicting views."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure

ROOT = 'https://www.zju.edu.cn/'
ORG = 'http://www.cmm.zju.edu.cn/55143/list.htm'
ACA = 'http://www.cmm.zju.edu.cn/55144/list.htm'


def body(section, content):
    return '<section class="about-content-box"><div class="content"><div id="wp_news_w5"><p><strong>' + section + '</strong></p><hr>' + content + '</div></div></section>'


def header(section, content):
    return '<div class="dropdown-item"><h3 class="dropdown-item-title">' + section + '</h3><ul class="dropdown-item-list"><li>' + content + '</li></ul></div>'


class MedicalDirectoryTests(unittest.TestCase):
    def parse(self, html, url=ORG):
        return extract_structure(html, url, ROOT, 'directory', '医学院目录')

    def test_text_list_preserves_separate_committees_and_unlinked_tail(self):
        html = body('学院委员会', '<p><span>人力资源委员会　学术委员会</span>　<span>教学委员会　学位委员会</span></p>')
        html += body('院系设置', '<p><a href="http://bms.zju.edu.cn/">基础医学院</a>&nbsp;&nbsp;<span>护理系</span></p>')
        nodes = {n['name']: n for n in self.parse(html)['nodes']}
        for name in ('人力资源委员会', '学术委员会', '教学委员会', '学位委员会'):
            self.assertEqual(nodes[name]['parent'], nodes['学院委员会']['key'])
            self.assertEqual(nodes[name]['url'], '')
        self.assertEqual(nodes['护理系']['parent'], nodes['院系设置']['key'])
        self.assertEqual(nodes['护理系']['url'], '')
        self.assertNotIn('人力资源委员会学术委员会教学委员会学位委员会', nodes)

    def test_inclusion_is_nested_but_shared_offices_and_aliases_are_not_split(self):
        html = body('职能部门', '<p>党政办公室（含国际交流与合作办公室、宣传中心）</p>'
                    '<p>发展规划与学科建设办公室（与党政办公室合署）</p><p>团委/学生工作办公室</p>'
                    '<p>发展联络办公室（医学发展部）</p>')
        result = self.parse(html)
        nodes = {n['name']: n for n in result['nodes']}
        for name in ('国际交流与合作办公室', '宣传中心'):
            self.assertEqual(nodes[name]['parent'], nodes['党政办公室']['key'])
            self.assertEqual(nodes[name]['relation'], 'nested_directory_entry')
        for name in ('发展规划与学科建设办公室（与党政办公室合署）', '团委/学生工作办公室', '发展联络办公室（医学发展部）'):
            self.assertEqual(nodes[name]['parent'], nodes['职能部门']['key'])
        self.assertNotIn('团委', nodes)
        self.assertNotIn('医学发展部', nodes)
        wording = [json.loads(n.split(':', 1)[1]) for n in result['notes'] if n.startswith('directory_relationship_wording:')]
        self.assertEqual(wording[0]['original'], '党政办公室（含国际交流与合作办公室、宣传中心）')

    def test_bare_parenthetical_list_does_not_inherit_explicit_subordination(self):
        html = body('职能部门', '<p>教学办公室（本科生教育办公室&nbsp;&nbsp;研究生教育办公室&nbsp;&nbsp;毕业后教育办公室）</p>')
        html += header('职能部门', '<p>教学办公室（副处级机构，下设：本科生教育办公室、研究生教育办公室、毕业后医学教育办公室）</p>')
        result = self.parse(html)
        undergraduates = [n for n in result['nodes'] if n['name'] == '本科生教育办公室']
        self.assertEqual({n['relation'] for n in undergraduates}, {'sub_unit', 'directory_companion_entry'})
        self.assertEqual(len({n['parent'] for n in undergraduates}), 2)
        variants = json.loads(next(n.split(':', 1)[1] for n in result['notes'] if n.startswith('directory_variant_differences:')))
        entry = next(v for v in variants if v['name'] == '本科生教育办公室')
        self.assertEqual({v['relationship'] for v in entry['versions']}, {'官网明确写明下设', '括号列示，隶属关系待核实'})
        self.assertTrue(any(v['name'] == '毕业后医学教育办公室' for v in variants))

    def test_same_website_with_different_names_is_not_renamed_or_merged(self):
        html = body('院系设置', '<p><a href="http://bms.zju.edu.cn/">基础医学院</a></p>')
        html += header('院系设置', '<p><a href="http://bms.zju.edu.cn/">基础医学系</a></p>')
        result = self.parse(html)
        nodes = {n['name']: n for n in result['nodes'] if n['kind'] == 'unit'}
        self.assertEqual(nodes['基础医学院']['url'], nodes['基础医学系']['url'])
        self.assertNotEqual(nodes['基础医学院']['key'], nodes['基础医学系']['key'])
        variants = json.loads(next(n.split(':', 1)[1] for n in result['notes'] if n.startswith('directory_variant_differences:')))
        self.assertEqual({v['name'] for v in variants}, {'基础医学院', '基础医学系'})

    def test_repeated_heading_is_group_and_leaf_is_unit_without_cycle(self):
        html = '<div class="departments_setup"><ul class="wp_subcolumn_list"><li class="wp_sublist"><h3 class="sublist_title">'
        html += '<a childcolumnid="1" href="https://www.zjuss.cn/" title="口腔医学院">口腔医学院</a></h3>'
        html += '<div class="items"><h2>{栏目名称}</h2><div class="college"><div class="text"><div class="departments"><a href="https://www.zjuss.cn/">口腔医学院</a></div></div></div></div></li></ul></div>'
        result = self.parse(html, ACA)
        entries = [n for n in result['nodes'] if n['name'] == '口腔医学院']
        self.assertEqual(len(entries), 2)
        group = next(n for n in entries if n['kind'] == 'group')
        unit = next(n for n in entries if n['kind'] == 'unit')
        self.assertEqual(unit['parent'], group['key'])
        self.assertEqual(group['url'], '')
        self.assertEqual(unit['url'], 'https://www.zjuss.cn/')
        self.assertTrue(all(n['key'] != n['parent'] for n in result['nodes']))
        self.assertFalse(any(n['name'] == '{栏目名称}' for n in result['nodes']))

    def test_similar_markup_on_another_page_is_not_medical_adapter_evidence(self):
        html = body('学院委员会', '<p>学术委员会　教学委员会</p>')
        result = self.parse(html, 'https://another.edu.cn/55143/list.htm')
        self.assertFalse(any(n['relation'] == 'directory_group' for n in result['nodes']))


if __name__ == '__main__':
    unittest.main()
