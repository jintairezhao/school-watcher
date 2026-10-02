"""Article images must use the same public transport as article collection."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from flask import render_template_string

import test_storage_management as fixtures


class ArticleImageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StorageManagementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.original = 'https://example.edu.cn/uploads/payment.png'
        self.ann = self.fixture.article(content_html=(
            '<p>扫码报名</p><a href="https://example.edu.cn/uploads/full.png">'
            f'<img src="{self.original}" alt="报名二维码"></a>'))

    def displayed_image(self):
        response = self.fixture.client.get(f'/api/announcements/{self.ann.id}/content')
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.json['content_html'], 'html.parser').img['src']

    def test_cached_api_and_template_use_same_local_image_endpoint(self):
        source = self.displayed_image()
        self.assertTrue(source.startswith(f'/api/announcements/{self.ann.id}/images/'), source)
        with self.fixture.app.test_request_context():
            rendered = render_template_string(
                "{% from '_content_loader.html' import content_loader %}{{ content_loader(ann) }}",
                ann=self.ann)
        body = BeautifulSoup(rendered, 'html.parser').select_one('.body-loader-content')
        self.assertEqual(body.img['src'], source)
        self.assertEqual(body.img['alt'], '报名二维码')
        self.assertEqual(body.a['href'], 'https://example.edu.cn/uploads/full.png')
        self.assertIn(self.original, self.ann.content_html)

    def test_image_bytes_use_public_transport_without_forwarding_remote_headers(self):
        source = self.displayed_image()
        payload = b'\x89PNG\r\n\x1a\n' + b'fixture'
        upstream = Mock(status_code=200, headers={
            'Content-Type': 'image/png', 'Set-Cookie': 'upstream-secret=value'})
        upstream.iter_content.return_value = iter([payload])
        with patch('backend.scraper.http_client.requests.get', return_value=upstream) as fetch:
            response = self.fixture.client.get(source)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, payload)
        self.assertEqual(response.mimetype, 'image/png')
        self.assertNotIn('upstream-secret', response.headers.get('Set-Cookie', ''))
        self.assertEqual(fetch.call_args.args[0], self.original)
        self.assertTrue(fetch.call_args.kwargs['stream'])
        upstream.close.assert_called_once()

    def test_arbitrary_url_and_unpublished_notice_are_not_image_proxies(self):
        source = self.displayed_image()
        with patch('backend.scraper.http_client.requests.get') as fetch:
            response = self.fixture.client.get(f'/api/announcements/{self.ann.id}/images/' + 'f' * 64,
                query_string={'url': 'http://127.0.0.1/private'})
            self.assertEqual(response.status_code, 404)
            self.fixture.school.enabled = False
            fixtures.db.session.commit()
            self.assertEqual(self.fixture.client.get(source).status_code, 404)
            fetch.assert_not_called()

    def test_relative_sources_resolve_to_original_page_and_unsafe_sources_are_removed(self):
        from backend.services.article_images import image_sources, render_article_html
        html = ('<img src="../qr.png"><img src="//cdn.example.edu.cn/qr.png">'
                '<img src="http://127.0.0.1/private"><img src="javascript:alert(1)">'
                '<img src="data:image/svg+xml,test"><img src="">')
        base = 'https://example.edu.cn/notices/article.html'
        sources = image_sources(html, base)
        self.assertEqual(set(sources.values()), {
            'https://example.edu.cn/qr.png', 'https://cdn.example.edu.cn/qr.png'})
        rendered = BeautifulSoup(render_article_html(html, self.ann.id, base), 'html.parser')
        self.assertEqual(len(rendered.select('img[src]')), 2)
        self.assertTrue(all(img['src'].startswith('/api/announcements/')
                            for img in rendered.select('img[src]')))

    def test_non_images_oversized_streams_and_network_failures_do_not_return_active_content(self):
        from requests import Timeout
        source = self.displayed_image()
        for mime, chunks in [('text/html', [b'<script>alert(1)</script>']),
                             ('image/svg+xml', [b'<svg onload="alert(1)"/>']),
                             ('image/png', [b'not a png']),
                             ('image/png', [b'\x89PNG\r\n\x1a\n', b'x' * (8 * 1024 * 1024)])]:
            with self.subTest(mime=mime, size=sum(map(len, chunks))):
                upstream = Mock(status_code=200, headers={'Content-Type': mime})
                upstream.iter_content.return_value = iter(chunks)
                with patch('backend.scraper.http_client.requests.get', return_value=upstream):
                    response = self.fixture.client.get(source)
                self.assertEqual(response.status_code, 502)
                upstream.close.assert_called_once()
        with patch('backend.scraper.http_client.requests.get', side_effect=Timeout):
            self.assertEqual(self.fixture.client.get(source).status_code, 502)


if __name__ == '__main__':
    unittest.main()
