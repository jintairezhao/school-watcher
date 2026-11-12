"""A split parenthetical label stays raw evidence, not a registered unit."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure

ROOT = 'https://example.edu.cn/'
FRAGMENT = '校友总会办公室（教育基金会办公室'


class IncompleteDirectoryNamesTests(unittest.TestCase):
    def parse(self, content):
        return extract_structure(content, ROOT + 'departments/', ROOT, 'directory', '管理与服务部门')

    def test_split_link_name_is_pending_and_its_original_link_is_retained(self):
        parsed = self.parse('<main><p><a href="/foundation/">' + FRAGMENT
                            + '</a>、<a href="/institute/">金陵研究院）</a></p></main>')
        fragment = next(n for n in parsed['nodes'] if n['name'] == FRAGMENT)
        self.assertEqual(fragment['relation'], 'directory_label_pending')
        self.assertEqual(fragment['url'], ROOT + 'foundation/')
        self.assertTrue(any(link['label'] == FRAGMENT and link['url'] == ROOT + 'foundation/' for link in parsed['links']))
        self.assertFalse(any(n['relation'] in ('directory_entry', 'nested_directory_entry')
                             and n['name'] in (FRAGMENT, '金陵研究院）') for n in parsed['nodes']))

    def test_nested_heading_list_does_not_reintroduce_the_fragment(self):
        parsed = self.parse('<main><h2>研究院</h2><ul><li><a href="/foundation/">'
                            + FRAGMENT + '</a></li></ul></main>')
        matches = [n for n in parsed['nodes'] if n['name'] == FRAGMENT]
        self.assertTrue(matches)
        self.assertTrue(all(n['relation'] == 'directory_label_pending' for n in matches))

    def test_complete_parenthetical_and_ordinary_names_remain_directory_members(self):
        names = ('校友总会办公室（教育基金会办公室）', '办公室(校友中心)', '研究生院', '党委研究生工作部')
        for name in names:
            with self.subTest(name=name):
                parsed = self.parse('<main><p><a href="/unit/">' + name + '</a></p></main>')
                self.assertTrue(any(n['name'] == name and n['relation'] == 'directory_entry' for n in parsed['nodes']))


if __name__ == '__main__':
    unittest.main()
