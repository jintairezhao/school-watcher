import unittest
from backend.scraper.acquisition.attachments import pdf_observation
from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result


class AttachmentAcquisitionTests(unittest.TestCase):
    def test_headers_or_filename_do_not_turn_a_denied_or_incomplete_response_into_pdf_evidence(self):
        url = 'https://example.edu.cn/notice.pdf'
        for payload in (b'<html>Login</html>', b'%PDF-1.7\ntruncated'):
            self.assertIsNone(pdf_observation(payload, url, {'Content-Type': 'application/pdf'}))
        observed = pdf_observation(b'%PDF-1.7\nfixture\n%%EOF\n', url, {})
        self.assertEqual(observed['filename'], 'notice.pdf')
        raw = FetchResult(url, status=403, attachment=observed)
        self.assertFalse(classify_result(FetchRequest(url, purpose='article'), raw).ok)

    def test_only_article_reads_accept_verified_files_and_rfc5987_filename_is_preserved(self):
        url = 'https://example.edu.cn/123456.pdf'
        observed = pdf_observation(b'%PDF-1.7\nfixture\n%%EOF\n', url, {
            'Content-Disposition': "attachment; filename*=UTF-8''%E9%80%9A%E7%9F%A5.pdf"})
        self.assertEqual(observed['filename'], '通知.pdf')
        raw = FetchResult(url, status=200, attachment=observed)
        self.assertTrue(classify_result(FetchRequest(url, purpose='article'), raw).ok)
        self.assertFalse(classify_result(FetchRequest(url, purpose='list'), raw).ok)
        self.assertEqual(len(observed['sha256']), 64)
