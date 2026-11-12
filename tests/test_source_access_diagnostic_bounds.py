"""Access diagnosis cannot leave the declared entries or send model requests."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from scripts.checks.run_source_access_diagnostic import DiagnosticBounds, allowed_address
from scripts.checks.run_native_branch_pilot import SamplingBoundary,read


def scope():
    return dict(expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(),
        allowed_hosts=['www.swpu.edu.cn'],targets=[dict(path='/sgy'),dict(path='/dky')],
        website=dict(allowed_hosts=['www.swpu.edu.cn'],maximum_documents=6,maximum_http_attempts=1,
                     maximum_response_bytes=3145728),model=dict(maximum_dispatch_attempts=0))


class SourceAccessDiagnosticBoundsTests(unittest.TestCase):
    def test_only_declared_public_entry_paths_and_redirects_are_admitted(self):
        for url in ('http://www.swpu.edu.cn/sgy','https://www.swpu.edu.cn/dky/index.htm'):
            self.assertTrue(allowed_address(scope(),url))
        for url in ('https://www.swpu.edu.cn/sgy-other','https://www.swpu.edu.cn/sgy/%2e%2e/jwb',
                    'https://other.swpu.edu.cn/sgy','https://www.swpu.edu.cn.evil.example/sgy',
                    'https://user:password@www.swpu.edu.cn/sgy'):
            self.assertFalse(allowed_address(scope(),url))

    def test_pinned_wire_attempt_is_counted_once_and_limit_stops_before_another(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);bounds=DiagnosticBounds(scope(),root,root/'web',time.monotonic()+60)
            self.assertEqual(bounds.http_attempt('http://192.0.2.1/sgy','GET',{'Host':'www.swpu.edu.cn'}),1)
            with self.assertRaises(SamplingBoundary):
                bounds.http_attempt('http://192.0.2.1/sgy','GET',{'Host':'www.swpu.edu.cn'})
            self.assertEqual(len(read(bounds.web_file)['http_attempts']),1)

    def test_external_host_and_non_get_do_not_reach_the_wire_ledger(self):
        for wire,method,host in [('https://192.0.2.1/chat/completions','POST','api.deepseek.com'),
                                 ('http://192.0.2.1/sgy','POST','www.swpu.edu.cn')]:
            with TemporaryDirectory() as temp:
                root=Path(temp);bounds=DiagnosticBounds(scope(),root,root/'web',time.monotonic()+60)
                with self.assertRaises(SamplingBoundary):bounds.http_attempt(wire,method,{'Host':host})
                self.assertEqual(read(bounds.web_file)['http_attempts'],[])
