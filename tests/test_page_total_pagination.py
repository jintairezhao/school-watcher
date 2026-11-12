"""Real retained page-count controls must keep their local list ownership."""
import gzip
import hashlib
from pathlib import Path
import unittest

from flask import Flask
from backend.database.db import db
from backend.scraper.engine import _next_page_url
from backend.services.source_validation import pagination_sample

URL = 'http://www.swpu.edu.cn/sgy/tzgg.htm'
SELECTOR = ('html:nth-of-type(1) > body:nth-of-type(1) > div:nth-of-type(2) > '
    'div:nth-of-type(1) > div:nth-of-type(2) > div:nth-of-type(2) > ul:nth-of-type(1) > '
    ':is(div.container > div.sub-right > div.sub-right-con > ul.list > li)')
HASH = '0048ecefca6d49046d7e9fb23a751576b632783a06d6b3a1e7416fca14a853c8'
ITEMS = '<li><a href="/info/one.htm">正式通知事项一</a></li><li><a href="/info/two.htm">正式通知事项二</a></li>'
PAGER = ('<div><span><a href="?p=2">2</a><a href="?p=3">3</a>'
    '<a href="?p=2">下页</a><a href="?p=27">尾页</a></span>'
    '<span>共27页</span><span>到第</span><input type="text"><span>页</span>'
    '<a href="javascript:;" onclick="gotoPage()">跳转</a></div>')


class MissingMaterial(BaseException):
    pass


class PageTotalPaginationTests(unittest.TestCase):
    def original(self):
        path = Path(__file__).parent / 'fixtures/structure/swpu-sgy-notices-0048ecef-20261102.html.gz'
        raw = gzip.decompress(path.read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), HASH)
        return raw.decode()

    def test_real_retained_notice_list_has_a_next_page(self):
        self.assertEqual(_next_page_url(self.original(), URL, 1, list_selector=SELECTOR),
                         'http://www.swpu.edu.cn/sgy/tzgg/26.htm')

    def test_native_validation_requests_the_missing_page_material(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite://')
        db.init_app(app)
        requests = []
        def missing(url, purpose):
            requests.append((url, purpose))
            raise MissingMaterial()
        with app.app_context(), self.assertRaises(MissingMaterial):
            pagination_sample(dict(name='通知公告', list_url=URL, list_selector=SELECTOR),
                [], self.original(), dict(url=URL), None, missing)
        self.assertEqual(requests, [('http://www.swpu.edu.cn/sgy/tzgg/26.htm', 'list')])

    def test_plain_total_page_controls_belong_to_their_actual_list(self):
        html = '<main><ul id="a">' + ITEMS + PAGER + '</ul><section><h2>邻栏</h2><ul id="b">' + ITEMS + '</ul></section></main>'
        self.assertEqual(_next_page_url(html, URL, 1, list_selector='#a > li'), URL + '?p=2')
        self.assertIsNone(_next_page_url(html, URL, 1, list_selector='#b > li'))

    def test_total_page_controls_cannot_hide_a_neighboring_article(self):
        for article in ('<a href="/article.htm">邻栏正式通知</a>',
                        '<a href="javascript:viewArticle()">阅读办理须知</a>'):
            html = '<main><ul id="a">' + ITEMS + '</ul><section>' + article + PAGER + '</section></main>'
            with self.subTest(article=article):
                self.assertIsNone(_next_page_url(html, URL, 1, list_selector='#a > li'))

    def test_page_count_alone_and_foreign_host_do_not_create_a_next_page(self):
        for pager in ('<div>共27页</div>',
                      PAGER.replace('href="?p=2"', 'href="https://other.edu.cn/p2.htm"')):
            html = '<main><ul id="a">' + ITEMS + '</ul><section>' + pager + '</section></main>'
            with self.subTest(pager=pager):
                self.assertIsNone(_next_page_url(html, URL, 1, list_selector='#a > li'))


class PageTotalGovernanceTests(unittest.TestCase):
    def test_missing_observed_page_material_blocks_proposal_validation(self):
        from tests.test_pagination_scope import SourcePaginationLifecycleTests, MIXED
        from backend.services import source_governance as governance
        fixture = SourcePaginationLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        source = fixture.source()
        first = MIXED.replace('</ul></section></aside>', '</ul>' + PAGER + '</section></aside>')
        config = governance.source_config(source)
        scope = dict(container_selector='#policies', heading_selector='#policies h2')
        def fetch(url, purpose):
            return first if purpose in ('list', 'independent_list') else fixture.body(url)
        captured = governance.capture_source_evidence(fixture.fixture.school_id, config,
            department_id=source.id, seed_html=first, scope_evidence=scope, fetcher=fetch)
        self.assertIn('pagination', captured.bundle)
        captured.bundle.pop('pagination')
        proposal = governance.propose_source(fixture.fixture.school_id, config, captured, department_id=source.id)
        result = governance.validate_proposal(proposal.id)
        self.assertIn('pagination_sample_missing', result['errors'])
        self.assertFalse(result['passed'])


if __name__ == '__main__':
    unittest.main()
