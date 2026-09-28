"""Regression coverage for untrusted imports, historical HTML and account revocation."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from werkzeug.security import generate_password_hash
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

    def login(self):
        client = self.fixture.app.test_client()
        with client.session_transaction() as state:
            state['_csrf_token'] = 'token'
        response = client.post('/login', data={'username':'admin', 'password':'original-password', 'csrf_token':'token'})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as state:
            state['_csrf_token'] = 'token'
        return client

    def clients(self):
        self.fixture.admin.password_hash = generate_password_hash('original-password')
        db.session.commit()
        return self.login(), self.login()

    def test_administrator_reset_revokes_old_cookie(self):
        old, admin = self.clients()
        response = admin.post(f'/api/admin/users/{self.fixture.admin.id}/reset-password', json={}, headers=self.fixture.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(old.get('/api/admin/stats').status_code, 401)

    def test_self_password_change_keeps_current_session_but_revokes_other(self):
        old, current = self.clients()
        response = current.post('/api/me/password', json={'current_password':'original-password','new_password':'new-password'}, headers=self.fixture.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(old.get('/api/admin/stats').status_code, 401)
        self.assertEqual(current.get('/api/admin/stats').status_code, 200)

    def test_recovery_proof_is_invalid_after_password_reset_and_after_use(self):
        from backend.auth import hash_answer
        old, admin = self.clients()
        self.fixture.admin.security_question = 'test question'
        self.fixture.admin.security_answer_hash = hash_answer('test answer')
        db.session.commit()
        recovery = self.fixture.app.test_client()
        with recovery.session_transaction() as state:
            state['_csrf_token'] = 'token'
        response = recovery.post('/api/recovery/verify', json={'username':'admin','answer':'test answer'}, headers=self.fixture.headers)
        self.assertEqual(response.status_code, 200)
        stale_cookie = recovery.get_cookie('session').value
        reset = recovery.post('/api/recovery/reset', json={'password':'recovered-password'}, headers=self.fixture.headers)
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(old.get('/api/admin/stats').status_code, 401)
        recovery.set_cookie('session', stale_cookie)
        self.assertEqual(recovery.post('/api/recovery/reset', json={'password':'replayed-password'}, headers=self.fixture.headers).status_code, 400)


if __name__ == '__main__':
    unittest.main()
