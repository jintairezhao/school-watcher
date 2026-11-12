"""Directory card headings scope their own roster, never adjacent cards or menus."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure

ROOT = 'https://example.edu.cn/'


def card(heading, label, url):
    return (f'<li><h3>{heading}</h3><div class="sub-con"><div><ul>'
            f'<li><a href="{url}">{label}</a></li></ul></div>'
            '<div class="image"></div></div></li>')


class WrappedDirectorySectionsTests(unittest.TestCase):
    def parse(self, html, label='人文社会科学学部'):
        return extract_structure(html, ROOT+'directory/', ROOT, 'directory', label)

    def parents(self, parsed, label):
        nodes = {n['key']:n for n in parsed['nodes']}
        return [nodes[n['parent']]['name'] for n in parsed['nodes']
                if n['name']==label and n['relation']!='navigation_entry']

    def test_wrapped_rosters_use_local_heading_instead_of_page_caption(self):
        html='<main><ul>'+card('植物科学学部','农学院','/agriculture/')
        html+=card('人文社会科学学部','经济管理学院','/economics/')+'</ul></main>'
        parsed=self.parse(html)
        self.assertEqual(self.parents(parsed,'农学院'),['植物科学学部'])
        self.assertEqual(self.parents(parsed,'经济管理学院'),['人文社会科学学部'])

    def test_two_explicit_card_memberships_are_preserved(self):
        html='<main><ul>'+card('植物科学学部','联合学院','/shared/')
        html+=card('生物与环境学部','联合学院','/shared/')+'</ul></main>'
        self.assertEqual(set(self.parents(self.parse(html),'联合学院')),
                         {'植物科学学部','生物与环境学部'})

    def test_repeated_header_links_remain_navigation(self):
        html='<header><h3>人文社会科学学部</h3><div><ul><li><a href="/agriculture/">农学院</a></li></ul></div></header>'
        html+='<main><ul>'+card('植物科学学部','农学院','/agriculture/')+'</ul></main>'
        parsed=self.parse(html)
        farm=[n for n in parsed['nodes'] if n['name']=='农学院']
        self.assertEqual(sum(n['relation']=='navigation_entry' for n in farm),1)
        self.assertEqual(self.parents(parsed,'农学院'),['植物科学学部'])


if __name__=='__main__':unittest.main()
