"""Regression coverage for untrusted imports, historical HTML and removed account routes."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from backend.database.db import db
from backend.database.models import Announcement
from backend.scraper.sanitizer import sanitize_html
import test_storage_management as storage_fixtures


ATTACKS = (
    '<a href="java&#9;script:window.__audit_marker=1">link</a>',
    '<a href="java&#10;script:window.__audit_marker=1">link</a>',
    '<svg><a xlink:href="javascript:window.__audit_marker=1"><text>link</text></a></svg>',
    '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=window.__audit_marker=1>">',
    '<img src="data:image/svg+xml,test" onerror="window.__audit_marker=1">',
    '<script>window.__audit_marker=1</script><iframe srcdoc="test"></iframe>',
)


def assert_inert(case, html):
    soup = BeautifulSoup(html, 'html.parser')
    case.assertIsNone(soup.find(['script', 'svg', 'math', 'iframe', 'object', 'embed', 'form']))
    for tag in soup.find_all(True):
        for key, value in tag.attrs.items():
            case.assertFalse(key.startswith('on'))
            case.assertNotIn(key, ('id', 'name', 'srcdoc', 'xlink:href'))
            if key in ('href', 'src'):
                compact = ''.join(str(value).split()).lower()
                case.assertFalse(compact.startswith(('javascript:', 'vbscript:', 'data:')))


class ArticleSecurityTests(unittest.TestCase):
    def test_html5_payloads_are_inert_after_repeated_round_trips(self):
        for payload in ATTACKS:
            with self.subTest(payload=payload):
                cleaned = sanitize_html(payload)
                assert_inert(self, cleaned)
                self.assertEqual(sanitize_html(cleaned), cleaned)

    def test_normal_notice_formatting_links_and_images_survive(self):
        html = '<h2>申请材料</h2><table><tr><td colspan="2">说明</td></tr></table><ul><li>材料</li></ul><a href="https://example.edu.cn/申请.pdf">附件</a><img src="https://example.edu.cn/image.png" alt="流程图">'
        soup = BeautifulSoup(sanitize_html(html), 'html.parser')
        self.assertEqual(soup.td['colspan'], '2')
        self.assertEqual(soup.img['alt'], '流程图')
        self.assertEqual(soup.a['href'], 'https://example.edu.cn/申请.pdf')
        self.assertEqual(soup.a['target'], '_blank')

    def test_styles_and_dom_attributes_cannot_impersonate_the_application(self):
        html = '<p id="discoveryLog" name="fetch" class="body-loader" data-content-id="1" style="position:fixed;background:url(https://evil.invalid);font-weight:bold;text-align:center">通知</p>'
        soup = BeautifulSoup(sanitize_html(html), 'html.parser')
        self.assertNotIn('id', soup.p.attrs)
        self.assertNotIn('class', soup.p.attrs)
        self.assertNotIn('data-content-id', soup.p.attrs)
        self.assertNotIn('position', soup.p.get('style', ''))
        self.assertNotIn('url(', soup.p.get('style', ''))
        self.assertIn('font-weight', soup.p['style'])


class ImportAndSessionSecurityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = storage_fixtures.StorageManagementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_import_cleans_active_html_before_storage(self):
        data = self.fixture.sample()
        data['announcements'][0]['content_html'] = ''.join(ATTACKS)
        response = self.fixture.upload(storage_fixtures.packed(data))
        self.assertEqual(response.status_code, 200)
        ann = Announcement.query.one()
        assert_inert(self, ann.content_html)

    def test_old_cached_html_is_cleaned_by_both_display_boundaries(self):
        ann = self.fixture.article(content_html=''.join(ATTACKS))
        response = self.fixture.client.get(f'/api/announcements/{ann.id}/content')
        self.assertEqual(response.status_code, 200)
        assert_inert(self, response.json['content_html'])
        from flask import render_template_string
        rendered = render_template_string("{% from '_content_loader.html' import content_loader %}{{ content_loader(announcement) }}", announcement=ann)
        body = BeautifulSoup(rendered, 'html.parser').select_one('.body-loader-content')
        assert_inert(self, str(body))

    def test_account_endpoints_are_absent_even_outside_native_window(self):
        routes = ('/login', '/register', '/logout', '/recovery', '/me',
                  '/api/recovery/question', '/api/recovery/verify', '/api/recovery/reset',
                  '/api/me/password', '/api/me/security', '/api/admin/users',
                  '/api/admin/users/1/reset-password', '/api/admin/users/1/role', '/api/admin/toggles')
        for path in routes:
            for method in ('GET', 'POST'):
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.fixture.client.open(path, method=method,
                        headers=self.fixture.headers).status_code, 404)
        self.assertNotIn('promote-user', self.fixture.app.cli.commands)
        self.assertFalse({'auth', 'account'} & self.fixture.app.blueprints.keys())


if __name__ == '__main__':
    unittest.main()
