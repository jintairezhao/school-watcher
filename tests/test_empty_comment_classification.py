"""Non-rendered template comments cannot prove that a real page is empty."""
import unittest

from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result


class EmptyCommentClassificationTests(unittest.TestCase):
    def test_comment_does_not_override_populated_directory(self):
        address='https://example.edu.cn/'
        for comment in ('No Data','暂无通知'):
            with self.subTest(comment=comment):
                html=('<title>研究生招生信息网</title><nav>'
                      '<a href="/masters/">硕士公告</a><a href="/doctoral/">博士公告</a>'
                      '</nav><div><!-- '+comment+' --></div>')
                result=classify_result(FetchRequest(address,purpose='directory'),
                    FetchResult(address,status=200,html=html))
                self.assertEqual(result.outcome,'usable')
                self.assertNotIn('explicit_empty_message',result.evidence)

    def test_comment_alone_cannot_verify_an_empty_page(self):
        address='https://example.edu.cn/'
        for purpose in ('directory','list'):
            with self.subTest(purpose=purpose):
                result=classify_result(FetchRequest(address,purpose=purpose),
                    FetchResult(address,status=200,html='<div><!-- No Data --></div>'))
                self.assertNotEqual(result.outcome,'empty')


if __name__=='__main__':unittest.main()
