"""Large observed pages must not lose entrances while model inputs stay bounded."""
import unittest

from bs4 import BeautifulSoup

from backend.ai.skill_loader import canonical
from backend.scraper.discovery.column_regions import materials


URL = 'https://example.edu.cn/student/'


class ColumnRegionTests(unittest.TestCase):
    def assert_coverage(self, html, *, maximum=24 * 1024):
        parts = materials(1, URL, html, max_bytes=maximum)
        self.assertTrue(parts)
        original = BeautifulSoup(html, 'lxml')
        actual = set()
        for part in parts:
            self.assertLessEqual(len(canonical(part).encode()), maximum)
            for evidence in part['evidence']:
                if evidence.get('original_root_selector'):
                    self.assertEqual(len(original.select(evidence['original_root_selector'])), 1)
                actual.update(a['href'] for a in BeautifulSoup(evidence.get('html', ''), 'lxml').select('a[href]'))
        self.assertEqual(actual, {a['href'] for a in original.select('a[href]')})
        return parts

    def test_large_plain_div_navigation_preserves_every_link(self):
        html = '<title>学生工作处</title><div id="ordinary-links">' + ''.join(
            f'<a href="/{i}/">校园服务入口{i}</a>' for i in range(700)) + '</div>'
        parts = self.assert_coverage(html)
        self.assertGreater(len(parts), 1)

    def test_oversized_list_keeps_real_roots_and_parent_heading(self):
        html = '<section><h2>本科生资助</h2><ul>' + ''.join(
            f'<li><a href="/info/{i}.htm">关于学生资助申请的通知{i}</a><span>2026-10-01</span></li>'
            for i in range(400)) + '</ul></section>'
        parts = self.assert_coverage(html, maximum=8 * 1024)
        original = BeautifulSoup(html, 'lxml')
        for part in parts:
            for item in part['evidence']:
                if '<li>' not in item.get('html', ''):
                    continue
                root = original.select_one(item['original_root_selector'])
                self.assertEqual(str(root), item['html'])
                self.assertIn('本科生资助', canonical(item.get('region_context', [])))

    def test_single_notice_after_long_text_is_not_dropped(self):
        html = '<main><div>' + '真实官网说明文字。' * 8000 + '</div>'
        html += '<section><h2>通知公告</h2><ul><li><a href="/notice/only.htm">唯一一条学生通知</a></li></ul></section></main>'
        parts = self.assert_coverage(html)
        text = ''.join(BeautifulSoup(item.get('html', ''), 'lxml').get_text()
                       for part in parts for item in part['evidence'])
        self.assertEqual(text.count('真实官网说明文字。'), 8000)

    def test_more_than_eight_small_column_regions_are_not_one_output_batch(self):
        html = '<main>' + ''.join('<section><h2>栏目' + str(i) + '</h2><ul><li><a href="/info/'
                                 + str(i) + '.htm">学生通知</a></li></ul></section>' for i in range(20)) + '</main>'
        parts = self.assert_coverage(html)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(sum(len(BeautifulSoup(e.get('html', ''), 'lxml').select('h2'))
                                for e in part['evidence']) <= 8 for part in parts))

    def test_pure_header_is_not_bought_again_as_a_column_region(self):
        html = '<title>学生工作处</title><div class="c-header_list_content_box">' + ''.join(
            f'<a href="/service/{i}">办事入口{i}</a>' for i in range(700)) + '</div>'
        html += '<main><h2>学生公告</h2><ul><li><a href="/info/1.htm">关于助学金申请的通知</a><time>2026-10-01</time></li></ul></main>'
        parts = materials(1, URL, html)
        self.assertEqual(len(parts), 1)
        self.assertNotIn('/service/', canonical(parts))
        self.assertIn('/info/1.htm', canonical(parts))
        self.assertIn('学生工作处', canonical(parts))

    def test_publication_items_inside_header_remain_observable(self):
        html = '<header><ul><li><a href="/info/1.htm">关于2026年学生奖学金申请材料提交的通知</a>' \
               '<time>2026-10-01</time></li></ul></header>'
        self.assert_coverage(html)

    def test_feedback_region_is_first_but_other_regions_are_preserved(self):
        html = '<main>' + ''.join('<section id="column' + str(i) + '"><h2>栏目' + str(i)
            + '</h2><ul><li><a href="/info/' + str(i) + '.htm">学生通知</a></li></ul></section>' for i in range(20)) + '</main>'
        parts = materials(1, URL, html, preferred_selectors=['#column19'])
        self.assertIn('/info/19.htm', canonical(parts[0]))
        self.assertEqual({a['href'] for p in parts for e in p['evidence']
                          for a in BeautifulSoup(e['html'], 'lxml').select('a[href]')},
                         {'/info/' + str(i) + '.htm' for i in range(20)})

    def test_oversized_single_anchor_keeps_its_observed_address(self):
        html = '<main><a href="/only.htm">' + '实际通知标题与说明。' * 4000 + '</a></main>'
        self.assert_coverage(html)


if __name__ == '__main__':
    unittest.main()
