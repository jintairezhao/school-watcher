"""Truncated official labels require explicit evidence; URL conflicts stay visible."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.directory_adapters import directory_variants
from bs4 import BeautifulSoup

URL = 'https://www.fudan.edu.cn/489/list.htm'
ROOT = 'https://www.fudan.edu.cn/'


def primary(text, href='https://ciram.fudan.edu.cn/'):
    return '<div class="col_news_list"><ul class="part_xy"><li class="item column-1">' \
           '<h3>院系专业</h3><div class="sub-con"><ul class="sub-list"><li>' \
           '<a class="sub-link" href="' + href + '"><span>&gt;</span>' + text + \
           '</a></li></ul></div></li></ul></div>'


def footer(text, href='https://ciram.fudan.edu.cn/', title=''):
    return '<div id="fddx_yxdw"><ul><li><span><a href="' + href + '" title="' + title + '">' \
           + text + '</a></span></li></ul></div>'


class DirectoryLabelTests(unittest.TestCase):
    def parse(self, html, url=URL):
        return extract_structure(html, url, ROOT, 'directory', '院系专业')

    def test_same_address_full_copy_completes_label_and_preserves_its_evidence(self):
        full = '智能机器人与先进制造创新学院'
        result = self.parse(primary('智能机器人与先进制造创新学...') + footer(full))
        group = next(n for n in result['nodes'] if n['relation'] == 'directory_group')
        entries = [n for n in result['nodes'] if n['parent'] == group['key']]
        self.assertEqual([n['name'] for n in entries], [full])
        self.assertFalse(any(n['name'].startswith('>') or n['name'].endswith('...') for n in result['nodes']))
        evidence = [json.loads(n.split(':', 1)[1]) for n in result['notes'] if n.startswith('directory_label_evidence:')]
        self.assertEqual(evidence[0]['displayed'], '智能机器人与先进制造创新学...')
        self.assertEqual(evidence[0]['full_label'], full)
        self.assertTrue(evidence[0]['reference_locators'])

    def test_conflicting_expansions_remain_pending_instead_of_picking_a_name(self):
        html = primary('智能机器人与先进...') + footer('智能机器人与先进制造创新学院')
        html += '<a class="sub-link" href="https://ciram.fudan.edu.cn/">智能机器人与先进技术研究院</a>'
        result = self.parse(html)
        pending = [n for n in result['nodes'] if n['relation'] == 'directory_label_pending']
        self.assertEqual([n['name'] for n in pending], ['智能机器人与先进...'])
        group = next(n for n in result['nodes'] if n['relation'] == 'directory_group')
        self.assertFalse(any(n['relation'] == 'directory_entry' and n['parent'] == group['key'] for n in result['nodes']))

    def test_different_addresses_and_placeholder_links_cannot_supply_a_missing_name(self):
        for target in ('https://other.fudan.edu.cn/', 'http://ciram.fudan.edu.cn/', '#'):
            with self.subTest(target=target):
                parsed = self.parse(primary('智能机器人与先进...') + footer('智能机器人与先进制造创新学院', target))
                self.assertTrue(any(n['relation'] == 'directory_label_pending' for n in parsed['nodes']))

    def test_own_title_can_complete_truncation_but_cannot_rename_a_full_label(self):
        parsed = self.parse(footer('资产管理处(实验室安全...', 'https://zcglc.fudan.edu.cn/', '资产管理处(实验室安全管理中心)'))
        self.assertTrue(any(n['name'] == '资产管理处(实验室安全管理中心)' for n in parsed['nodes']))
        parsed = self.parse(footer('物理学系', 'https://phys.fudan.edu.cn/', '现代物理研究所'))
        self.assertTrue(any(n['name'] == '物理学系' for n in parsed['nodes']))
        self.assertFalse(any(n['name'] == '现代物理研究所' for n in parsed['nodes']))

    def test_directory_variants_preserve_different_urls_and_missing_links(self):
        html = primary('法医学与法庭科学学院', 'https://sfms.fudan.edu.cn/')
        html += '<li class="sub-item i5-1-1"><a class="sub-link" href="https://sfms.fudan.edu.cn/"><span>&gt;</span>法医学与法庭科学学院</a></li>'
        html += footer('法医学与法庭科学学院', '#')
        variants = directory_variants(BeautifulSoup(html, 'lxml'), URL)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]['name'], '法医学与法庭科学学院')
        versions = {v['name']: v['urls'] for v in variants[0]['versions']}
        self.assertEqual(versions['正文院系目录'], ['https://sfms.fudan.edu.cn/'])
        self.assertEqual(versions['顶部院系导航'], ['https://sfms.fudan.edu.cn/'])
        self.assertEqual(versions['页脚院系目录'], [''])
        untouched = self.parse(html, ROOT + 'other.htm')
        self.assertFalse(any(n['relation'] == 'directory_group' for n in untouched['nodes']))


if __name__ == '__main__':
    unittest.main()
