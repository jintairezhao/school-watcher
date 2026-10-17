"""A failed region needs its real surrounding DOM, not a new whole-site scan."""
import unittest
from bs4 import BeautifulSoup

from backend.ai.skill_loader import canonical
from backend.scraper.discovery.column_regions import materials, repair_scope_selectors


URL = 'https://example.edu.cn/'


class RepairScopeTests(unittest.TestCase):
    def test_repair_keeps_actual_heading_list_and_original_positions(self):
        html = '<main><section><h2 id="heading">通知公告</h2><div id="items">'
        html += '<a href="/only.pdf">附件通知</a></div></section><section>'
        html += '<h2>其他栏目</h2>' + '与本次失败无关的完整材料。' * 3000 + '</section></main>'
        observations = [{'container_selector': '#items', 'heading_selector': '#heading',
                         'list_selector': '#missing li'}]
        roots = repair_scope_selectors(html, observations)
        self.assertTrue(roots)
        parts = materials(1, URL, html, scope_selectors=roots, compact_locations=True)
        text = canonical(parts)
        self.assertIn('通知公告', text)
        self.assertIn('/only.pdf', text)
        self.assertNotIn('与本次失败无关', text)
        original = BeautifulSoup(html, 'lxml')
        for part in parts:
            for proof in part['evidence']:
                self.assertEqual(str(original.select_one(proof['original_root_selector'])), proof['html'])
        self.assertEqual(len(parts), 1)

    def test_any_unlocatable_failure_falls_back_instead_of_discarding_it(self):
        html = '<main><section id="one"><h2>通知公告</h2></section><section id="two">其他栏目</section></main>'
        roots = repair_scope_selectors(html, [{'container_selector': '#one'},
                                            {'container_selector': '#missing'}])
        self.assertEqual(roots, ())
        self.assertIn('其他栏目', canonical(materials(1, URL, html, scope_selectors=roots)))

    def test_ambiguous_cross_column_matches_cannot_be_mistaken_for_a_small_scope(self):
        html = '<body><section><h2>甲</h2><ul><li>甲通知</li></ul></section>'
        html += '<section><h2>乙</h2><ul><li>乙通知</li></ul></section></body>'
        self.assertEqual(repair_scope_selectors(html, [{'container_selector': 'section'}]), ())

    def test_same_page_multiple_failed_regions_empty_and_attachment_all_remain(self):
        html = '<main><section id="one"><h2>附件公告</h2><a href="/notice.pdf">附件</a></section>'
        html += '<section id="two"><h2>空栏目</h2><p>暂无内容</p></section></main>'
        roots = repair_scope_selectors(html, [{'container_selector': '#one'}, {'container_selector': '#two'}])
        text = canonical(materials(1, URL, html, scope_selectors=roots))
        self.assertIn('/notice.pdf', text)
        self.assertIn('暂无内容', text)

    def test_ordinary_exploration_still_retains_every_later_candidate(self):
        html = '<main>' + ''.join('<section><h2>栏目' + str(i) + '</h2><a href="/'
                    + str(i) + '.htm">真实通知</a></section>' for i in range(30)) + '</main>'
        text = canonical(materials(1, URL, html))
        self.assertIn('/29.htm', text)


if __name__ == '__main__':
    unittest.main()
