"""Literal article PDF-player references; no page or resource requests."""
import unittest
from unittest.mock import patch

from backend.scraper.acquisition import FetchRequest,FetchResult,fetch

from backend.scraper.acquisition.embedded_pdf import reference,valid_binding

URL='https://example.edu.cn/notices/one.htm'
PLAYER='<div class="wp_pdf_player" pdfsrc="/files/notice.pdf"></div>'


class PDFSourceReferenceTests(unittest.TestCase):
    def setUp(self):
        forbidden=AssertionError('PDF reference checks forbid network and databases')
        for target in ('socket.getaddrinfo','socket.socket.connect','sqlite3.connect'):
            guard=patch(target,side_effect=forbidden);guard.start();self.addCleanup(guard.stop)

    def test_visible_article_pdfsrc_has_hash_and_dom_binding(self):
        html='<div class="wp_articlecontent">'+PLAYER+'</div>'
        observed=reference(html,URL)
        self.assertIsNotNone(observed)
        self.assertEqual(observed['requested_url'],'https://example.edu.cn/files/notice.pdf')
        self.assertEqual(observed['literal_url'],'/files/notice.pdf')
        self.assertEqual(observed['reference_kind'],'pdfsrc_attribute')
        self.assertTrue(valid_binding(dict(embedding=observed,resource_url=observed['requested_url']),html,URL))

    def test_hidden_external_ambiguous_and_nonarticle_pdfsrc_are_not_references(self):
        for html in ('<div hidden class="wp_articlecontent">'+PLAYER+'</div>',
                     '<div class="wp_articlecontent">'+PLAYER.replace('/files/notice.pdf','https://elsewhere.edu.cn/file.pdf')+'</div>',
                     '<div class="wp_articlecontent">'+PLAYER+PLAYER.replace('notice.pdf','other.pdf')+'</div>',
                     '<nav>'+PLAYER+'</nav>',
                     '<div class="wp_articlecontent"><!--'+PLAYER+'--></div>'):
            with self.subTest(html=html):self.assertIsNone(reference(html,URL))

    def test_existing_script_loader_binding_keeps_its_original_schema(self):
        html='<div class="v_news_content"><script>showVsbpdfIframe("/files/old.pdf",1,2,3,4,5);</script></div>'
        observed=reference(html,URL)
        self.assertIsNotNone(observed)
        self.assertEqual(set(observed),{'version','page_url','content_hash','locator','literal_url','requested_url'})
        self.assertTrue(valid_binding(dict(embedding=observed,resource_url=observed['requested_url']),html,URL))


class PDFSourceAcquisitionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_embedded_pdf_acquisition import EmbeddedPDFAcquisitionTests
        self.fixture=EmbeddedPDFAcquisitionTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.article=self.fixture.data['articles'][0]
        self.parent='https://www.swpu.edu.cn/material-fixture/wp-parent.htm'
        self.resource='https://www.swpu.edu.cn/material-fixture/notice.pdf'
        self.fixture.documents[self.resource]=self.fixture.documents[self.article['pdf_url']]
        self.html='<h1>'+self.article['title']+'</h1><div class="wp_articlecontent"><div pdfsrc="'+self.resource+'"></div></div>'
        self.fixture.documents[self.parent]=self.html
        forbidden=AssertionError('Acquisition integration uses retained fixture bytes, never network or DB')
        for target in ('socket.getaddrinfo','socket.socket.connect','sqlite3.connect'):
            guard=patch(target,side_effect=forbidden);guard.start();self.addCleanup(guard.stop)

    def request(self):
        return FetchRequest(self.parent,purpose='article',browser_allowed=False,policy={'expected_article_title':self.article['title']})

    def test_pdfsrc_player_resolves_resource_even_when_parent_region_is_recognized(self):
        result=fetch(self.request(),http_transport=self.fixture.transport)
        self.assertEqual(self.fixture.calls,[self.parent,self.resource])
        self.assertTrue(result.ok)
        self.assertEqual(result.html,self.html)
        self.assertEqual(result.attachment['sha256'],self.article['pdf']['sha256'])
        self.assertEqual(result.attachment['embedding']['reference_kind'],'pdfsrc_attribute')

    def test_denied_pdf_keeps_parent_html_and_the_access_failure(self):
        def transport(request):
            if request.url==self.resource:
                self.fixture.calls.append(request.url)
                return FetchResult(self.resource,status=403,html='')
            return self.fixture.transport(request)
        result=fetch(self.request(),http_transport=transport)
        self.assertEqual(result.outcome,'denied')
        self.assertEqual(result.html,self.html)
        self.assertIn('embedded_pdf_read_incomplete',result.evidence)


if __name__=='__main__':unittest.main()
