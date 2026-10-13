import unittest
from backend.scraper.acquisition import FetchRequest, FetchResult, fetch
from backend.scraper.acquisition.coordinator import execution_context
from backend.scraper.http_client import inventory_read_attempt

URL='https://example.edu.cn/notices/'
HTML='<html><title>示例大学通知公告</title><body><nav><a href="/teaching/">教学通知</a><a href="/aid/">学生资助</a></nav></body></html>'

class NetworkBrowserRecoveryTests(unittest.TestCase):
    def test_second_scheduled_read_uses_browser_without_an_extra_http_request(self):
        calls=[]
        def http(request):
            calls.append('http');return FetchResult(URL,outcome='network_error',error_code='http_connect_timeout')
        def browser(request):
            calls.append('browser');return FetchResult(URL,status=200,html=HTML,transport='browser')
        with execution_context(browser_dispatch=browser),inventory_read_attempt(URL,0):
            first=fetch(FetchRequest(URL),http_transport=http)
        self.assertEqual(first.outcome,'network_error')
        with execution_context(browser_dispatch=browser),inventory_read_attempt(URL,1):
            second=fetch(FetchRequest(URL),http_transport=http)
        self.assertTrue(second.ok)
        self.assertEqual(calls,['http','browser'])

    def test_network_recovery_does_not_bypass_access_denial(self):
        def browser(request):
            return FetchResult(URL,status=403,html='<title>Access denied</title><body>Forbidden</body>')
        with execution_context(browser_dispatch=browser),inventory_read_attempt(URL,1):
            result=fetch(FetchRequest(URL),http_transport=lambda r:self.fail('Extra HTTP request'))
        self.assertFalse(result.ok)
        self.assertIn(result.outcome,('denied','needs_manual'))

    def test_disabled_browser_retains_the_http_retry(self):
        with (execution_context(browser_dispatch=lambda r:self.fail('Disabled browser')),
              inventory_read_attempt(URL,1)):
            result=fetch(FetchRequest(URL,browser_allowed=False),http_transport=lambda r:FetchResult(
                URL,outcome='network_error',error_code='http_connect_timeout'))
        self.assertEqual(result.outcome,'network_error')

    def test_another_page_on_the_same_host_does_not_inherit_failed_attempt(self):
        with (execution_context(browser_dispatch=lambda r:self.fail('Unfailed page')),
              inventory_read_attempt(URL,1)):
            result=fetch(FetchRequest('https://example.edu.cn/other/'),http_transport=lambda r:FetchResult(
                r.url,status=200,html=HTML))
        self.assertTrue(result.ok)
