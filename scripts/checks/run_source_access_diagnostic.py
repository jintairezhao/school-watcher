"""Bounded public-entry route diagnosis; no crawl, model, source or task writes."""
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest.mock import patch
import argparse
import hashlib
import json
import os
import posixpath
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.checks.run_native_branch_pilot import Bounds, SamplingBoundary, bounded_process, read, save, sha


def allowed_address(scope, url):
    parsed = urlsplit(str(url))
    path = posixpath.normpath(unquote(parsed.path or '/'))
    return bool(parsed.scheme in ('http','https') and parsed.hostname in scope['allowed_hosts']
        and not parsed.username and not parsed.password and parsed.port in (None,80,443)
        and any(path == target['path'] or path.startswith(target['path']+'/') for target in scope['targets']))


class DiagnosticBounds(Bounds):
    def check_address(self, url):
        if not allowed_address(self.scope, url):
            self.stop('outside_declared_entry', url=str(url))

    def new_document(self, request):
        self.check_address(request.url)
        return super().new_document(request)

    def http_attempt(self, url, method, headers):
        host = headers.get('Host') or headers.get('host') or urlsplit(str(url)).hostname
        parsed = urlsplit(str(url))
        logical = parsed._replace(netloc=host or '').geturl()
        self.check_address(logical)
        if method.upper() != 'GET':
            self.stop('non_get_request')
        return super().http_attempt(url, method, headers)


def run_diagnostic(scope_path, output, deadline):
    scope, output = read(scope_path), Path(output)
    os.environ.update(WATCHER_ENV_FILE=str(output/'no-env-read'),
        WATCHER_DATA_DIR=str(output/'unused-data'), WATCHER_SEED_ON_START='0')
    from requests.utils import get_environ_proxies, select_proxy
    from bs4 import BeautifulSoup
    from backend.ai.material_capture import capture_materials
    from backend.scraper.acquisition import FetchRequest, fetch
    from backend.scraper.acquisition import http_transport
    from backend.scraper.http_client import proxy_required
    from backend.services.discovery_policy import get_policy
    assert get_policy()['html_max_bytes'] <= scope['website']['maximum_response_bytes']
    bounds = DiagnosticBounds(scope, output/'bounded-observations', output/'web-materials', deadline)
    original_validate = http_transport.validate_public_url
    rows, skipped = [], []
    reason = 'declared_variants_complete'
    required = proxy_required()

    def validate(url, **kwargs):
        bounds.check_address(url)
        return original_validate(url, **kwargs)

    def observe(target, route, url):
        bounds.check_time()
        request = FetchRequest(url, purpose='directory', browser_allowed=False,
            source_id='access-diagnostic:'+target['id'], timeout_seconds=min(35,max(.001,deadline-time.monotonic())))
        with ExitStack() as stack:
            if route.startswith('explicit_direct'):
                assert not required, 'A required proxy must not be bypassed'
                stack.enter_context(patch.object(http_transport,'get_environ_proxies',return_value={}))
            result = fetch(request)
        record = result.to_dict()
        html = record.pop('html','')
        soup = BeautifulSoup(html,'lxml') if html else None
        record['headers'] = {k:v for k,v in record.get('headers',{}).items()
            if k.lower() in ('content-type','location','retry-after','etag','last-modified')}
        record.update(content_hash=hashlib.sha256(html.encode()).hexdigest() if html else None,
                      page_title=soup.title.get_text(' ',strip=True) if soup and soup.title else '')
        rows.append(dict(target_id=target['id'],route=route,requested_url=url,
            final_coordinator_result=record,diagnostic_not_native_discovery=True,
            page_equivalence_or_source_validation_inferred=False))
        save(output/'observations.json',dict(rows=rows,skipped=skipped))
        return result

    with bounds.install(), patch.object(http_transport,'validate_public_url',side_effect=validate), \
            patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model in access diagnosis')), \
            patch('backend.ai.providers.complete',side_effect=AssertionError('No model in access diagnosis')), \
            patch('requests.Session.send',side_effect=AssertionError('Only bounded aiohttp GET is allowed')), \
            capture_materials(output/'execution-materials',label='two-entry-access-diagnostic') as capture:
        try:
            for target in scope['targets']:
                results = []
                if not required:
                    results.append(observe(target,'explicit_direct',target['http_url']))
                else:
                    skipped.append(dict(target_id=target['id'],variant='explicit_direct',reason='proxy_required'))
                proxy_selected = bool(select_proxy(target['http_url'],get_environ_proxies(target['http_url'])))
                if proxy_selected:
                    results.append(observe(target,'configured_proxy_route',target['http_url']))
                else:
                    skipped.append(dict(target_id=target['id'],variant='configured_proxy_route',reason='no_distinct_proxy_route'))
                if not required and results and not any(result.ok for result in results):
                    observe(target,'explicit_direct_https_candidate',target['https_candidate'])
                else:
                    skipped.append(dict(target_id=target['id'],variant='https_candidate',reason='http_readable_or_direct_not_permitted'))
        except SamplingBoundary as boundary:
            reason = str(boundary)
        finally:
            save(output/'observations.json',dict(rows=rows,skipped=skipped))
    save(output/'child-outcome.json',dict(reason=reason,completed_observations=len(rows),
        proxy_required=required,proxy_credentials_retained=False,model_calls=0,
        browser_calls=0,production_or_sampling_instance_writes=0))
    save(output/'capture-status.json',dict(path=str(capture.path),complete=capture.complete,
        scope='Diagnostic recording, not native exploration or source validation'))


def check_ready(scope_path):
    scope_path = Path(scope_path).resolve()
    assert scope_path.is_relative_to(ROOT/'.local')
    scope = read(scope_path)
    assert scope['maximum_wall_seconds']==240 and scope['model']['maximum_dispatch_attempts']==0
    assert scope['website']['maximum_documents']==6 and scope['website']['maximum_http_attempts']==12
    assert len(scope['targets'])==2 and all(allowed_address(scope,t['http_url']) and
        allowed_address(scope,t['https_candidate']) for t in scope['targets'])
    for name,digest in scope['protected_fingerprints'].items():
        assert sha(ROOT/name)==digest, 'Protected case or closed ledger changed'
    for name,digest in scope['code_fingerprints'].items():
        assert sha(ROOT/name)==digest, 'Diagnostic code changed since review'
    return scope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope',type=Path,required=True)
    parser.add_argument('--run',action='store_true')
    args = parser.parse_args()
    scope = check_ready(args.scope)
    if not args.run:
        print(json.dumps(dict(prepared=True,no_requests_started=True,maximum_documents=6,
            maximum_http_attempts=12,model_calls=0)))
        return
    approval = read(ROOT/scope['approval_record'])
    assert approval['approved'] and approval['scope_sha256']==sha(args.scope)
    assert datetime.now(timezone.utc)<datetime.fromisoformat(scope['expires_at'])
    output = (ROOT/scope['output_directory']).resolve()
    assert output.is_relative_to(ROOT/'.local') and not output.exists(), 'Inspect an existing run; never restart blindly'
    output.mkdir(parents=True)
    result = bounded_process(run_diagnostic,(str(args.scope.resolve()),str(output),time.monotonic()+240),240)
    result.update(model_calls=0,production_or_sampling_instance_writes=0)
    save(output/'process-outcome.json',result)
    assert all(sha(ROOT/p)==h for p,h in scope['protected_fingerprints'].items())
    print(json.dumps(result))


if __name__=='__main__':
    main()
