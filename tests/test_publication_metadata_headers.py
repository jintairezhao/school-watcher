"""Header families and misleading dates; no network or model needed."""
import unittest
import json
from pathlib import Path

from backend.services.publication_dates import inspect_article_date


class PublicationHeaderTests(unittest.TestCase):
    title = '关于开展研究生奖学金申报工作的通知'
    url = 'https://fixture.example.edu/20261016/notice.html'

    def inspect(self, html):
        return inspect_article_date(html, self.url, self.title)

    def verified(self, html, expected='2026-09-30'):
        proof = self.inspect(html)
        self.assertEqual((proof['state'], proof.get('date')), ('verified', expected), proof)
        self.assertTrue(proof['locator'])
        self.assertTrue(proof['content_sha256'])

    def test_semantic_and_custom_title_families(self):
        for tag in ('h1', 'h4', 'h5', 'h6', 'div', 'p', 'td'):
            with self.subTest(tag=tag):
                header = f'<{tag}>{self.title}</{tag}><div>日期：2026-09-30</div>'
                self.verified(f'<article>{header}<p>报名截止：2026-10-16</p></article>')

    def test_bare_date_beside_title_and_table_header_rows(self):
        self.verified(f'<article><header><h1>{self.title}</h1><p>2026/09/30</p></header><p>完整正文。</p></article>')
        self.verified(f'<table><tr><td>{self.title}</td></tr><tr><td>发布时间：2026-09-30 作者：甲</td></tr><tr><td>正文中的日期2026-10-16。</td></tr></table>')

    def test_split_header_editorial_and_date_fields(self):
        self.verified(f'<article><div><h3>{self.title}</h3></div><!-- metadata --><div>作者：甲 编辑：乙 时间：<span>2026-09-30</span> 点击量：3</div><div>活动时间：2026-10-16</div></article>')
        self.verified(f'<article><h2>{self.title}</h2><i>时间：2026-09-30 浏览：3</i><p>完整正文。</p></article>')

    def test_heading_tag_can_hold_the_metadata_row(self):
        self.verified(f'<article><h2>{self.title}</h2><h3>发布时间：2026-09-30 作者：甲</h3><p>完整正文。</p></article>')

    def test_english_publication_field(self):
        self.verified(f'<article><h1>{self.title}</h1><div>Source: Release:2026-09-30 16:28:40 Writer: Hits:</div><p>Event date:2026-10-16.</p></article>')

    def test_decorative_quote_is_a_title_only_inside_header(self):
        self.verified(f'<article><header><div><blockquote>{self.title}</blockquote></div><div>2026-09-30</div></header><p>完整正文。</p></article>')
        self.assertEqual(self.inspect(f'<article><blockquote><h1>{self.title}</h1><p>发布时间：2026-09-30</p></blockquote></article>')['state'], 'unknown')

    def test_repeated_body_title_does_not_mask_a_unique_header(self):
        self.verified(f'<article><h1>{self.title}</h1><span>2026-09-30</span><section><h2>{self.title}</h2><p>申报要求如下。</p></section></article>')

    def test_two_independent_article_headers_remain_ambiguous(self):
        article = f'<article><h1>{self.title}</h1><span>2026-09-30</span><p>正文。</p></article>'
        self.assertEqual(self.inspect(article + article)['state'], 'unknown')

    def test_publication_field_is_distinct_from_update_and_event_in_same_row(self):
        self.verified(f'<article><h1>{self.title}</h1><div>发布时间：2026-09-30 更新时间：2026-10-01 活动时间：2026-10-16</div></article>')

    def test_update_activity_deadline_and_short_year_do_not_become_publication(self):
        for value in ('更新时间：2026-09-30', '活动时间：2026-09-30', '截止时间：2026-09-30',
                      '发布时间：26-09-30', '09-30', '时间：2026-09-30 11:50—13:20'):
            with self.subTest(value=value):
                self.assertEqual(self.inspect(f'<article><h1>{self.title}</h1><div>{value}</div></article>')['state'], 'unknown')

    def test_prose_quotes_sidebar_hidden_and_other_articles_cannot_supply_date(self):
        for region in ('<p>请参考其他通知，发布日期：2026-09-30。</p>',
                       '<blockquote>发布时间：2026-09-30</blockquote>',
                       '<aside>发布时间：2026-09-30</aside>',
                       '<div hidden>发布时间：2026-09-30</div>',
                       '<div class="event-date"><span>2026-09-30</span></div>',
                       '<div><span>更新时间：</span><span>2026-09-30</span></div>',
                       '<section><h2>另一篇通知</h2><p>发布时间：2026-09-30</p></section>',
                       '<p>申报要求如下。</p><span>2026-09-30</span>'):
            with self.subTest(region=region):
                self.assertEqual(self.inspect(f'<article><h1>{self.title}</h1>{region}</article>')['state'], 'unknown')

    def test_conflicting_publication_fields_remain_unknown(self):
        for fields in ('<div>发布时间：2026-09-30</div><div>发布日期：2026-10-01</div>',
                       '<div>发布时间：2026-09-30 发布日期：2026-10-01</div>'):
            self.assertEqual(self.inspect(f'<article><h1>{self.title}</h1>{fields}</article>')['state'], 'unknown')

    def test_unrelated_head_metadata_without_visible_title_is_not_accepted(self):
        self.assertEqual(self.inspect('<html><head><meta name="PubDate" content="2026-09-30"></head><body><h1>另一篇通知</h1></body></html>')['state'], 'unknown')

    def test_retained_real_official_header_regions(self):
        evidence = json.loads((Path(__file__).parent / 'acceptance/publication_header_evidence_20261016.json').read_text('utf-8'))
        for sample in evidence['samples']:
            with self.subTest(url=sample['source_url']):
                result = inspect_article_date(sample['observed_header_html'], sample['source_url'], sample['title'])
                self.assertEqual((result['state'], result.get('date')),
                                 ('verified', sample['expected_publication_date']))


if __name__ == '__main__':
    unittest.main()
