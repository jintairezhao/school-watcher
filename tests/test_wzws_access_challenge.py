"""A real access-check shell is not a failed source-content parser.

The browser-marked cases are metadata contract fixtures over retained bytes,
not claims of an actual browser visit or successful challenge completion.
"""
from pathlib import Path
import gzip
import hashlib
import unittest

from backend.scraper.acquisition.classifier import classify_result
from backend.scraper.acquisition.contracts import FetchRequest, FetchResult

ROOT_URL='https://www.ucass.edu.cn/'
WARNING='<noscript><h1>Please enable JavaScript and refresh the page.</h1></noscript>'
SCRIPT='<script src="/wzws-waf-cgi/json2.js"></script>'
SHELL='<html><head><title></title></head><body>'+WARNING+SCRIPT+'</body></html>'


class WzwsChallengeTests(unittest.TestCase):
    def result(self,html=SHELL,transport='http',purpose='directory',selector=''):
        return classify_result(FetchRequest(ROOT_URL,purpose=purpose,readiness_selector=selector),
            FetchResult(final_url=ROOT_URL,status=200,html=html,transport=transport))

    def test_actual_retained_http_challenge_is_access_not_generic_javascript(self):
        path=Path(__file__).parent/'fixtures/acquisition/ucass-wzws-home-20261023.html.gz'
        raw=gzip.decompress(path.read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
            '00602e8243867e604ca9aef595c75522c7d31c0dc5e4f0d2ed305c286e21321c')
        for purpose in ('directory','list','article'):
            with self.subTest(purpose=purpose):
                result=self.result(raw.decode('utf-8'),purpose=purpose)
                self.assertEqual(result.outcome,'requires_render')
                self.assertEqual(result.error_code,'access_challenge')
                self.assertIn('automatic_challenge',result.evidence)

    def test_same_shell_after_rendering_needs_manual_access_review(self):
        result=self.result(transport='browser')
        self.assertEqual(result.outcome,'needs_manual')
        self.assertEqual(result.error_code,'access_challenge')
        self.assertNotIn('来源规则',result.message)

    def test_access_shell_cannot_pass_a_matching_selector(self):
        result=self.result(selector='body')
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code,'access_challenge')

    def test_readable_article_with_the_library_is_not_blocked(self):
        html=SHELL.replace('</body>',
            '<article><h1>公开通知</h1><p>2026年10月23日。请符合条件的同学在截止日期前提交申请材料，详细安排以正文为准。</p></article></body>')
        result=self.result(html,purpose='article',selector='article')
        self.assertEqual(result.outcome,'usable')
        self.assertNotEqual(result.error_code,'access_challenge')

    def test_real_navigation_with_javascript_help_remains_usable(self):
        html=SHELL.replace('</body>','<nav><a href="/directory">院系设置</a></nav></body>')
        result=self.result(html,selector='nav')
        self.assertEqual(result.outcome,'usable')

    def test_quoted_script_path_is_not_an_active_access_gate(self):
        html='<article><h1>使用说明</h1><p>示例路径 /wzws-waf-cgi/json2.js ，请开启 JavaScript 以读取动态交互。</p></article>'
        self.assertEqual(self.result(html,purpose='article',selector='article').outcome,'usable')

    def test_cross_origin_script_does_not_certify_this_origin_access_gate(self):
        html=SHELL.replace('/wzws-waf-cgi/json2.js','https://other.example/wzws-waf-cgi/json2.js')
        self.assertNotEqual(self.result(html).error_code,'access_challenge')

    def test_javascript_help_without_waf_runtime_keeps_generic_classification(self):
        self.assertNotEqual(self.result(SHELL.replace(SCRIPT,'<script src="app.js"></script>')).error_code,'access_challenge')

    def test_runtime_without_the_specific_empty_shell_is_not_enough(self):
        self.assertNotEqual(self.result(SHELL.replace(WARNING,'')).error_code,'access_challenge')


if __name__=='__main__':unittest.main()
