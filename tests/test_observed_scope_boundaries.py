"""Independent scope-recovery regressions for ambiguous publication boundaries."""
from copy import deepcopy
import unittest
from pathlib import Path
from bs4 import BeautifulSoup

from backend.services.source_governance import _column_scope
from backend.services.source_scope import recover_observed_scope
from tests.test_observed_scope_recovery import CONFIG, URL, WRONG_SCOPE, cards, page


class ObservedScopeBoundaryTests(unittest.TestCase):
    def assert_unrepaired(self, html, config=None):
        config = deepcopy(config or CONFIG)
        original = deepcopy(config)
        reference = {'url': URL}
        _, errors, _ = _column_scope(config, reference, WRONG_SCOPE, html=html)
        self.assertEqual(errors, ['column_identity_or_scope_unconfirmed'])
        correction = recover_observed_scope(config, reference, WRONG_SCOPE, html=html)
        self.assertEqual(config, original)
        self.assertIsNone(correction)

    def test_neighbour_column_with_an_ordinary_label_cannot_borrow_a_self_link(self):
        for label in ('<div class="column-label">学校新闻</div>',
                      '<a class="column-label" href="/news/">学校新闻</a>'):
            with self.subTest(label=label):
                html = ('<main><section><nav><a href="/announcements/">通知公告</a></nav></section>'
                        '<section>' + label + cards() + '</section></main>')
                self.assert_unrepaired(html)

    def test_a_container_with_multiple_articles_cannot_be_approved_as_one_record(self):
        html = page()
        config = dict(CONFIG, list_selector='div.cards')
        records, _, _ = _column_scope(config, {'url': URL}, WRONG_SCOPE, html=html)
        self.assertEqual(len(records), 1)
        self.assert_unrepaired(html, config)

    def test_another_plain_publication_list_does_not_need_a_known_cms_url(self):
        other = '<section><div class="column-label">学校新闻</div><ul>' + ''.join(
            f'<li><a href="/stories/event-{i}.html">另一个栏目本学期关于组织活动的通知{i}</a>'
            '<span>2026-10-15</span></li>' for i in range(3)) + '</ul></section>'
        self.assert_unrepaired(page(other))

    def test_direct_text_in_the_shared_region_can_mark_a_neighbour_column(self):
        html = ('<main><nav><a href="/announcements/">通知公告</a></nav>学校新闻'
                + cards() + '</main>')
        self.assert_unrepaired(html)

    def test_an_unselected_differently_shaped_record_is_not_hidden_inside_list_parent(self):
        html = (Path(__file__).parent / 'fixtures/acquisition/retained-cupk-announcements.html').read_text('utf-8')
        soup = BeautifulSoup(html, 'lxml')
        soup.select_one('.SecondArticle__art').append(BeautifulSoup(
            '<div class="other-card"><a href="/stories/other.html">关于学生工作相关事项的通知</a>'
            '<span class="date">2026-10-15</span></div>', 'html.parser').div)
        config = dict(CONFIG, list_url='https://www.cupk.edu.cn/tzgg/',
            list_selector='div.SecondArticle__art--art', title_selector='h3.SecondArticle__art--titles',
            link_selector='a.SecondArticle__art--link', date_selector='div.SecondArticle__dates')
        _, errors, _ = _column_scope(config, {'url': config['list_url']}, WRONG_SCOPE, html=str(soup))
        self.assertEqual(errors, ['column_identity_or_scope_unconfirmed'])
        self.assertIsNone(recover_observed_scope(config, {'url': config['list_url']}, WRONG_SCOPE, html=str(soup)))


if __name__ == '__main__':
    unittest.main()
