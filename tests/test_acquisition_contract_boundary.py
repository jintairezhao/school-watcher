"""Real call boundaries: the acquisition contract and the errors crossing it.

These tests deliberately patch the *transport* (``fetch_or_raise`` or the legacy
page dictionary) rather than the functions under test, so a mismatch between what
a caller asks for and what the acquisition contract accepts cannot hide behind a
replaced helper. The zero-onboarding defect this guards was exactly such a
mismatch: ``source_governance`` asked for purpose ``body`` while the contract only
accepts ``directory``/``list``/``article``, and every verification task died on a
``ValueError`` that no handler upstream of the transport expected.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import School, User
from backend.database.source_governance_models import SourceProposal
from backend.services import source_governance as governance


class AcquisitionContractTests(unittest.TestCase):
    """The frozen transport contract, asserted directly."""

    def test_request_rejects_a_purpose_the_transport_cannot_honour(self):
        from backend.scraper.acquisition import FetchRequest
        # 'body' is the evidence-role vocabulary. It must never reach the wire.
        for purpose in ('body', 'independent_list', 'page', ''):
            with self.assertRaisesRegex(ValueError, 'Unknown acquisition purpose'):
                FetchRequest(url='https://www.example.edu.cn/', purpose=purpose)
        for purpose in ('directory', 'list', 'article'):
            self.assertEqual(FetchRequest(url='https://www.example.edu.cn/', purpose=purpose).purpose, purpose)

    def test_evidence_roles_are_translated_to_transport_purposes(self):
        from backend.scraper.acquisition import FetchRequest
        seen = []
        def transport(request):
            seen.append(request)
            from backend.scraper.acquisition import FetchResult
            return FetchResult(request.url, status=200, html='<html><body>ok</body></html>', outcome='usable')
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=transport):
            governance._fetch('https://www.example.edu.cn/n/', 'body')
            governance._fetch('https://www.example.edu.cn/n/', 'list')
            governance._fetch('https://www.example.edu.cn/n/', 'independent_list')
        self.assertEqual([r.purpose for r in seen], ['article', 'list', 'list'])
        # The independent pass must stay distinguishable from a routine refetch.
        self.assertEqual([r.policy.get('verification_pass') for r in seen], [None, None, 'independent'])

    def test_every_purpose_reaching_the_wire_is_one_the_contract_accepts(self):
        from backend.scraper.acquisition import FetchRequest
        seen = []
        def transport(request):
            seen.append(request.purpose)
            from backend.scraper.acquisition import FetchResult
            return FetchResult(request.url, status=200, html='<html><body>ok</body></html>', outcome='usable')
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=transport):
            for role in ('body', 'list', 'independent_list'):
                governance._fetch('https://www.example.edu.cn/n/', role)
        # Rebuilding the request is the assertion: an unknown purpose raises here.
        for purpose in seen:
            FetchRequest(url='https://www.example.edu.cn/', purpose=purpose)


class FetchFailureCrossingTests(unittest.TestCase):
    """A typed transport failure must become a review state, not a crash."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'boundary-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite://',
            'SOURCE_INVENTORY_PATH': Path(self.tmp.name) / 'inventory.sqlite3',
            'SOURCE_GOVERNANCE_EVIDENCE_PATH': Path(self.tmp.name) / 'evidence',
            'SOURCE_CATALOG_PATH': Path(self.tmp.name) / 'catalog.sqlite3'})
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.school = School(name='示例大学', url='https://www.example.edu.cn/')
        db.session.add_all([self.school, User(username='admin', password_hash='unused', role='admin')])
        db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def submitted(self):
        """A user-submitted entry: no selectors yet, so the list is fetched first."""
        return governance.propose_source(self.school.id,
            {'name': '通知公告', 'list_url': 'https://college.example.edu.cn/notices/'},
            origin='submitted_entry')

    def test_typed_fetch_failure_is_recorded_instead_of_escaping(self):
        from backend.scraper.acquisition import FetchFailure, FetchResult
        proposal = self.submitted()
        denied = FetchResult('https://college.example.edu.cn/notices/', status=403,
                             outcome='denied', error_code='access_denied_page',
                             message='官网拒绝本次访问；已有通知仍保留')
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=FetchFailure(denied)):
            result = governance.process_source_review({'proposal_id': proposal.id})
        db.session.refresh(proposal)
        self.assertEqual(proposal.state, 'needs_review')
        self.assertEqual(result['state'], 'needs_review')
        errors = json.loads(proposal.validation_json)['errors']
        self.assertTrue(any('拒绝' in str(e) for e in errors), errors)
        # The task must not be left mid-flight for the queue to retry blindly.
        self.assertEqual(proposal.validator_version, governance.VERSION)

    def test_transport_crash_is_distinguished_from_a_typed_refusal(self):
        from backend.scraper.acquisition import FetchFailure, FetchResult
        proposal = self.submitted()
        with patch('backend.scraper.acquisition.fetch_or_raise',
                   side_effect=ValueError('Unknown acquisition purpose')):
            with self.assertRaises(ValueError):
                governance.process_source_review({'proposal_id': proposal.id})
        db.session.rollback()
        # A contract violation is a program fault: it must surface, never be
        # silently rebranded as a source that merely needs review.
        self.assertNotEqual(db.session.get(SourceProposal, proposal.id).state, 'needs_review')

    def test_fetch_source_page_keeps_the_callers_purpose(self):
        """The legacy page dictionary must not relabel an article as a directory."""
        from backend.services.source_collection import fetch_source_page
        from backend.scraper.acquisition import FetchFailure
        # Many navigation links, no dated anchor, no article region.
        nav_only = '<html><body><nav>' + ''.join(
            f'<a href="/s/{i}/">学院与机构设置入口{i}</a>' for i in range(12)) + '</nav></body></html>'
        legacy = {'url': 'https://www.example.edu.cn/s/', 'status': 200, 'html': nav_only}
        with patch('backend.scraper.discovery.inventory_crawler.fetch_page', return_value=legacy):
            page = fetch_source_page('https://www.example.edu.cn/s/', purpose='directory')
            self.assertTrue(page)
            # An article request must reject this page; a directory request accepts it.
            with self.assertRaises(FetchFailure):
                fetch_source_page('https://www.example.edu.cn/s/', purpose='article')


class LoginWallTests(unittest.TestCase):
    """The site's own login requirement is an access limit, not unparsed content.

    Judging this as ``content_not_recognized`` told the user to check extraction
    rules for a notice that simply requires an account.

    Every clause of the detector must be a property of the document itself. An
    address word is never enough, and the negative cases below are not
    hypothetical: with the looser test this replaced, a public notice list reached
    through a portal hop on a path containing an authentication word, carrying a
    generic navigation parameter, was reported as a login wall -- refusing a
    column the public can read. Real notice lists carry no article region and real
    homepages link to their own identity provider, so neither of those
    corroborations can be trusted alone either.
    """

    IDENTITY = ('<html><head><title>示例大学统一身份认证</title></head><body>'
                '<div>统一身份认证 UsernamePassword</div></body></html>')

    def classify(self, requested, landed, html, purpose='article'):
        from backend.scraper.acquisition.classifier import classify_result
        from backend.scraper.acquisition import FetchRequest, FetchResult
        return classify_result(FetchRequest(requested, purpose=purpose),
                               FetchResult(landed, status=200, html=html, outcome='usable'))

    def wall(self, landed, html=None, requested=None, purpose='article'):
        result = self.classify(requested or 'https://www.example.edu.cn/tzgg.htm',
                               landed, html or self.IDENTITY, purpose)
        return result.error_code == 'source_login_required'

    def listed(self, count=9, extra=''):
        """A public notice list: dated links, and no article region anywhere."""
        return ('<html><head><title>通知公告</title></head><body>'
                '<div class="login"><a href="/user/login">统一身份认证登录</a></div>'
                '<section id="notices"><h2>通知公告</h2><ul>' + ''.join(
                    f'<li><a href="tzggcontent.jsp?wbnewsid={i}">关于做好本学期工作的通知{i}</a>'
                    f'<span>09-2{i}</span></li>' for i in range(count)) + '</ul></section>' + extra + '</body></html>')

    def hand_off(self):
        return self.classify('https://www.example.edu.cn/tzggcontent.jsp?wbnewsid=1',
            'https://id.example.edu.cn/cas/login?service=https%3A%2F%2Fwww.example.edu.cn%2Fauth',
            self.IDENTITY)

    def test_a_single_sign_on_hand_off_is_reported_as_a_login_limit(self):
        result = self.hand_off()
        self.assertEqual((result.outcome, result.error_code), ('denied', 'source_login_required'))
        self.assertFalse(result.ok)
        # The user must not be sent to check extraction rules for this.
        self.assertNotIn('解析规则', result.message)

    def test_the_limit_reads_as_an_access_limit_not_a_rule_problem(self):
        from backend.services.inbox_refresh import source_status_kind, source_status_label
        result = self.hand_off()
        self.assertEqual((source_status_kind('failed', result.message, result.error_code),
                          source_status_label('failed', result.message, result.error_code)),
                         ('access_limited', '访问受限'))

    def test_sign_in_routes_are_recognised_without_naming_any_vendor(self):
        # Vendor-specific spellings: Shanghai Jiao Tong and the two teaching
        # platforms that call their route login_slogin. A wall missed here is
        # reported to the user as a parser problem instead of an access limit.
        for landed in ('https://jaccount.example.edu.cn/jaccount/jalogin?returl=https%3A%2F%2Fwww.example.edu.cn%2Ftzgg',
                       'https://jwgl.example.edu.cn/xtgl/login_slogin.html?service=%2Fxtgl%2Findex.html'):
            with self.subTest(landed=landed):
                self.assertTrue(self.wall(landed), landed)
        # The standard Chinese wording for single sign-on, which the narrower
        # word list missed.
        self.assertTrue(self.wall('https://id.example.edu.cn/cas/login?service=/tzgg/',
            '<html><body>统一身份认证 单点登录 用户名 密码 登录</body></html>'))

    def test_a_known_limit_a_hand_off_whose_last_segment_is_an_index(self):
        """Deliberately narrow: the document must *be* the sign-in route.

        /sso/index is a plausible sign-in entry and is not recognised, because
        the same shape describes a public /sso/index and the costs are not
        symmetric: a miss costs a less precise label, while a false positive
        refuses a column the public can read. Recorded here so the limit is
        visible rather than discovered later as a surprise.
        """
        self.assertFalse(self.wall('https://id.example.edu.cn/sso/index?return_url=/tzgg/'))

    def test_a_public_list_behind_a_portal_hop_is_not_a_login_wall(self):
        """The attack that the looser detector could not survive.

        Every clause had a public page that satisfied it: the hop path contained
        an authentication word, the parameter was a generic one, and a real notice
        list carries no article region. Read through that hop, a column the public
        can read would have been refused as inaccessible -- a new way to produce
        the zero-onboarding this work exists to remove.
        """
        page = self.listed()
        for landed in ('https://portal.example.edu.cn/sso/notice/list.html',
                       'https://portal.example.edu.cn/login/guest/notices.html',
                       'https://portal.example.edu.cn/signin/column/x',
                       'https://portal.example.edu.cn/login/guest/list.html?target=%2Ftzgg%2F',
                       'https://www.example.edu.cn/news/login.html'):
            with self.subTest(landed=landed):
                self.assertFalse(self.wall(landed, page, purpose='list'), landed)
        # The same document at its own address is ordinary readable content.
        self.assertEqual(self.classify('https://www.example.edu.cn/tzgg/', 'https://www.example.edu.cn/tzgg/',
                                       page, 'list').outcome, 'usable')

    def test_generic_navigation_parameters_are_not_a_sign_on_hand_off(self):
        # continue/goto/target are filters and return-to values on ordinary lists.
        for query in ('?continue=1', '?goto=page2', '?target=%2Ftzgg%2F'):
            with self.subTest(query=query):
                self.assertFalse(self.wall('https://id.example.edu.cn/cas/login' + query))

    def test_a_homepage_linking_to_its_identity_provider_is_not_a_wall(self):
        # Real university homepages carry "统一身份认证登录" in their own header.
        home = ('<html><head><title>示例大学</title></head><body><header>'
                '<a href="https://id.example.edu.cn/cas/login?service=https%3A%2F%2Fwww.example.edu.cn%2F">统一身份认证登录</a>'
                '</header><main>' + '<p>学校概况与新闻。</p>' * 600 + '</main></body></html>')
        self.assertFalse(self.wall('https://www.example.edu.cn/login/index.html', home, purpose='list'))

    def test_a_silent_oauth_hand_off_is_not_a_login_wall(self):
        # WeChat authorises publicly readable pages silently, in scope snsapi_base,
        # and returns to the page it was asked for. The landing document is not an
        # identity provider, so the hand-off address alone must not decide this.
        result = self.classify('https://www.example.edu.cn/tzgg/',
            'https://open.weixin.qq.com/connect/oauth2/authorize?redirect_uri=https%3A%2F%2Fwww.example.edu.cn%2Ftzgg%2F&scope=snsapi_base',
            '<html><body>请稍候</body></html>')
        self.assertNotEqual(result.error_code, 'source_login_required')

    def test_an_address_containing_a_login_word_is_not_an_identity_provider(self):
        # /news/login-guide.html is an ordinary publication path.
        result = self.classify('https://www.example.edu.cn/news/login-guide.html',
                               'https://www.example.edu.cn/news/login-guide.html', self.IDENTITY)
        self.assertNotEqual(result.error_code, 'source_login_required')

    def test_an_authentication_route_alone_is_not_enough(self):
        # A help page about single sign-on is not the sign-on page itself.
        result = self.classify('https://www.example.edu.cn/sso/help',
                               'https://www.example.edu.cn/sso/help',
                               '<html><body><p>关于单点登录的使用说明</p></body></html>')
        self.assertNotEqual(result.error_code, 'source_login_required')

    def test_a_readable_article_behind_an_authentication_address_is_content(self):
        # Address and heading both suggest an identity provider, but the document
        # carries a real body, so it is content and must be read as such.
        body = '<article><h1>通知</h1><p>' + '请符合条件的同学认真阅读本通知。' * 12 + '</p></article>'
        result = self.classify('https://www.example.edu.cn/cas/login?service=x',
                               'https://www.example.edu.cn/cas/login?service=x',
                               '<html><head><title>统一身份认证</title></head><body>' + body + '</body></html>')
        self.assertNotEqual(result.error_code, 'source_login_required')
        self.assertEqual(result.outcome, 'usable')

    def test_a_provider_page_is_a_wall_however_much_text_it_carries(self):
        # The provider's own page carries no password field -- it renders its form
        # with script -- and the real one runs past 13000 characters of visible
        # text. This test is the guard for the size limit that used to stand in the
        # detector: a page this large was refused as content, and the notice
        # reached the reader as a rule problem. The length assertion below is what
        # makes that boundary explicit, so the limit cannot come back unnoticed.
        from backend.scraper.acquisition.classifier import _IDENTITY_OPENING
        from bs4 import BeautifulSoup
        wall = ('<html><head><title>中国科学技术大学统一身份认证</title></head><body>'
                '<div>中国科学技术大学统一身份认证 UsernamePassword webauthn</div>'
                '<script>var challenge="' + 'A' * 40000 + '";</script>'
                '<p>' + '登录后可访问该系统。' * 900 + '</p></body></html>')
        soup = BeautifulSoup(wall, 'lxml')
        for node in soup.select('script, style, template, noscript'):
            node.decompose()
        self.assertGreater(len(soup.get_text(' ', strip=True)), 20 * _IDENTITY_OPENING)
        self.assertTrue(self.wall('https://id.example.edu.cn/cas/login?service=https%3A%2F%2Fwww.example.edu.cn%2Ftzgg.htm', wall))

    def test_a_notice_page_carrying_the_provider_link_is_read_not_refused(self):
        # The real shape of a readable notice: the department's header links to
        # the identity provider, and the notice itself sits in an article region.
        # The provider's wording is present, so presence alone cannot decide it.
        notice = ('<html><head><title>关于公布入围名单的通知 : 示例大学教务处</title></head><body>'
                  '<header><a href="https://id.example.edu.cn/cas/login?service=x">统一身份认证</a></header>'
                  '<article><h1>关于公布入围名单的通知</h1><p>'
                  + '请入围同学按通知要求完成后续事项。' * 60 + '</p></article></body></html>')
        result = self.classify('https://www.example.edu.cn/notice/notice-info/20515.html',
                               'https://www.example.edu.cn/notice/notice-info/20515.html', notice)
        self.assertEqual(result.outcome, 'usable')
        self.assertNotEqual(result.error_code, 'source_login_required')


class TransportFailureMessageTests(unittest.TestCase):
    """A reader is told what happened, not handed the transport's own words."""

    def error(self, exc):
        from backend.scraper.fetch_errors import describe_fetch_error
        described = describe_fetch_error(exc)
        return str(described), described

    def test_a_tls_failure_does_not_put_openSSL_text_in_front_of_a_reader(self):
        import requests
        # The shape a real TLS hand-off failure takes, as it reached a reader.
        raw = requests.exceptions.SSLError(
            "HTTPSConnectionPool(host='id.example.edu.cn', port=443): Max retries exceeded "
            "with url: /cas/login?service=x (Caused by SSLError(SSLEOFError(8, "
            "'[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol')))")
        message, described = self.error(raw)
        self.assertNotIn('SSLError', message)
        self.assertNotIn('HTTPSConnectionPool', message)
        self.assertNotIn('Caused by', message)
        self.assertTrue(described.retryable)
        # The technical cause is kept for diagnosis rather than thrown away.
        self.assertIs(described.__cause__, raw)

    def test_a_timeout_still_reads_as_advice(self):
        import requests
        message, described = self.error(requests.Timeout())
        self.assertIn('稍后重试', message)
        self.assertIsNone(described.__cause__)


class SourceStatusKindTests(unittest.TestCase):
    """Acceptance criterion 4: an access limit and a program fault differ."""

    def kind(self, state, message, code):
        from backend.services.inbox_refresh import source_status_kind, source_status_label
        return source_status_kind(state, message, code), source_status_label(state, message, code)

    def test_official_site_limits_are_not_reported_as_program_faults(self):
        from backend.scraper.acquisition.classifier import classify_result
        from backend.scraper.acquisition import FetchRequest, FetchResult
        # Codes the classifier really emits for a challenge and for a refusal.
        challenge = classify_result(FetchRequest('https://www.example.edu.cn/', purpose='list'),
            FetchResult('https://www.example.edu.cn/', status=202, outcome='usable',
                        html='<script>$_ts={};</script>'))
        denied = classify_result(FetchRequest('https://www.example.edu.cn/', purpose='list'),
            FetchResult('https://www.example.edu.cn/', status=403, outcome='usable',
                        html='<html><body>denied</body></html>'))
        self.assertEqual(self.kind('failed', challenge.message, challenge.error_code),
                         ('manual_verification', '需要验证'))
        self.assertEqual(self.kind('failed', denied.message, denied.error_code),
                         ('access_limited', '访问受限'))

    def test_program_and_parser_faults_are_reported_as_rule_problems(self):
        from backend.scraper.acquisition.classifier import classify_result
        from backend.scraper.acquisition import FetchRequest, FetchResult
        unrecognized = classify_result(FetchRequest('https://www.example.edu.cn/', purpose='list'),
            FetchResult('https://www.example.edu.cn/', status=200, outcome='usable',
                        html='<html><body><p>普通页面</p></body></html>'))
        self.assertEqual(self.kind('failed', unrecognized.message, unrecognized.error_code),
                         ('needs_adapter', '待适配'))
        self.assertEqual(self.kind('failed', 'HTTP 404', 'http_404'), ('address_gone', '地址失效'))
        self.assertEqual(self.kind('failed', '官网访问正在排队', 'origin_busy'), ('throttled', '请求受限'))

    def test_healthy_states_keep_their_own_labels(self):
        self.assertEqual(self.kind('pending', '', ''), ('pending', '等待更新'))
        self.assertEqual(self.kind('running', '', ''), ('running', '更新中'))
        self.assertEqual(self.kind('unloaded', '', ''), ('unloaded', '待采集'))
        self.assertEqual(self.kind('done', '', '')[1], '')


if __name__ == '__main__':
    unittest.main()
