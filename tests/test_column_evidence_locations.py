"""Evidence CSS is shorter only when it selects the identical observed node."""
import unittest
from bs4 import BeautifulSoup
from backend.scraper.discovery.column_regions import _compact_locations, materials
from backend.ai.skill_loader import canonical
from backend.services import tasks
from unittest.mock import patch


class ColumnEvidenceLocationsTests(unittest.TestCase):
    def test_duplicate_ids_and_identical_siblings_still_select_each_distinct_node(self):
        soup = BeautifulSoup('<main><section id="duplicate"><h2>通知</h2></section>'
            '<section id="duplicate"><h2>通知</h2></section></main>', 'lxml')
        locate = _compact_locations(soup)
        for node in soup.find_all(True):
            selected = soup.select(locate(node))
            self.assertEqual(len(selected), 1)
            self.assertIs(selected[0], node)

    def test_escaped_observed_identifiers_are_css_data(self):
        soup = BeautifulSoup('<main><section id="a:b"><p class="x:y">说明</p></section>'
                             '<section><p class="x:y">其他</p></section></main>', 'lxml')
        locate = _compact_locations(soup)
        for node in soup.find_all(True):
            self.assertIs(soup.select(locate(node))[0], node)

    def test_clearing_chrome_does_not_create_false_identifier_uniqueness(self):
        html = '<html><head><script id="shared"></script></head><body><svg>'
        html += '<a class="same">图标</a></svg><main><h2 id="shared">通知公告</h2>'
        html += '<a class="same" href="/1.htm">真实通知</a></main></body></html>'
        original = BeautifulSoup(html, 'lxml')
        soup = BeautifulSoup(html, 'lxml')
        locate = _compact_locations(soup)
        soup.head.clear(); soup.svg.clear()
        for node in (soup.h2, soup.main.a):
            selected = original.select(locate(node))
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0].get_text(), node.get_text())

    def test_shorter_metadata_keeps_original_subtree_and_links(self):
        html = '<html><body><div><div class="frame"><section class="notice"><h2>通知公告</h2>'
        html += '<ul>' + ''.join(f'<li><a href="/{i}.htm">真实通知标题{i}</a><time>2026-10-12</time></li>'
                                for i in range(6)) + '</ul></section></div></div></body></html>'
        old = materials(1, 'https://example.edu.cn/', html, compact_locations=False)
        new = materials(1, 'https://example.edu.cn/', html, compact_locations=True)
        self.assertLess(len(canonical(new).encode()), len(canonical(old).encode()))
        self.assertEqual([row['original_root_selector'] for part in old for row in part['evidence']],
                         [row['original_root_selector'] for part in new for row in part['evidence']])
        self.assertEqual([row['html'] for part in old for row in part['evidence']],
                         [row['html'] for part in new for row in part['evidence']])

    def test_only_explicit_new_checkpoint_uses_compact_metadata(self):
        html = '<main><section class="notice"><h2>通知公告</h2><ul><li><a href="/1.htm">'
        html += '真实完整通知标题</a><time>2026-10-12</time></li></ul></section></main>'
        legacy = materials(1, 'https://example.edu.cn/', html, compact_locations=False)
        compact = materials(1, 'https://example.edu.cn/', html, compact_locations=True)
        with patch.object(tasks, 'current_execution', return_value={'checkpoint': {'column_regions': {'cursor': 2}}}):
            self.assertEqual(materials(1, 'https://example.edu.cn/', html), legacy)
        with patch.object(tasks, 'current_execution', return_value={'checkpoint': {'column_evidence_layout': 'compact-locations-1'}}):
            self.assertEqual(materials(1, 'https://example.edu.cn/', html), compact)


if __name__ == '__main__':
    unittest.main()
