"""Reproduce engine observations without AI or touching user data.

Modes: retained = reclassify complete historical public HTML (no new web read);
fixtures = installed Edge on local fixtures; public = explicit observed-URL
manifest, isolated database, authenticated shared origin broker and no AI.
This measures structure and source-entry observation, not article extraction,
source installation, complete-school recall, API efficiency, or production fit.
No browser/model/dependency installation is performed by this command.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def summary(request, result, expected):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(result.html, 'lxml')
    checks = {selector: len(soup.select(selector)) == count for selector, count in expected.items()}
    return {'url': request.url, 'purpose': request.purpose, 'status': result.status,
            'outcome': result.outcome, 'error_code': result.error_code,
            'html_bytes': len(result.html.encode('utf-8')),
            'html_sha256': hashlib.sha256(result.html.encode('utf-8')).hexdigest(),
            'title': soup.title.get_text(' ', strip=True) if soup.title else '',
            'links': len(soup.select('a[href]')), 'timings': result.timings,
            'selector_checks': checks, 'expected_structure_present': all(checks.values()),
            'evidence': result.evidence}


def entry_regions(html, expected):
    """Compare actual entry identity and region, not just a count of anchors."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    observed = {selector: [(a.get_text(' ', strip=True), a.get('href'))
                           for a in soup.select(selector)] for selector in expected}
    return observed, {selector: observed[selector] == wanted for selector, wanted in expected.items()}


def retained(args, out):
    from backend.scraper.acquisition.contracts import FetchRequest
    from backend.scraper.acquisition.crawl4ai_transport import normalize_observation
    original = args.retained_directory.resolve()
    rows, hashes = [], {}
    for engine, filename, prefix in (('crawl4ai', 'results.json', ''),
                                      ('current-browser', 'current-results.json', 'current-')):
        report_file = original / filename
        hashes[str(report_file)] = hashlib.sha256(report_file.read_bytes()).hexdigest()
        for row in json.loads(report_file.read_text(encoding='utf-8')):
            html_file = original / (prefix + row['label'] + '.html')
            if not html_file.is_file():
                rows.append({'engine': engine, 'url': row['url'], 'state': 'material_not_retained'})
                continue
            content = html_file.read_bytes()
            hashes[str(html_file)] = hashlib.sha256(content).hexdigest()
            request = FetchRequest(row['url'], purpose='directory')
            observation = {'url': row['url'], 'status_code': row.get('status'),
                           'success': row.get('success', row.get('state') == 'done'),
                           'html': content.decode('utf-8')}
            result = normalize_observation(request, observation)
            rows.append(dict(summary(request, result, {}), engine=engine,
                             historical_seconds=row.get('seconds'),
                             original_material_sha256=hashes[str(html_file)],
                             state='retained_complete_html_reclassified'))
    unchanged = all(hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
                    for name, digest in hashes.items())
    if not unchanged:
        raise RuntimeError('Original comparison artifacts changed')
    return {'mode': 'retained_html_replay', 'rows': rows,
            'original_material_unchanged': unchanged, 'original_file_sha256': hashes,
            'new_website_reads': 0, 'actual_model_requests': 0,
            'not_evidence_of_new_online_engine_performance': True}


async def fixtures(args, out):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from backend.browser_service.runtime import BrowserRuntime
    from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
    from backend.scraper.acquisition.crawl4ai_transport import (
        Crawl4AIFixtureTransport, fixture_validator)
    from playwright.async_api import async_playwright
    calls = []
    cases = [
        ('root', '/', 'directory', 'nav a', {'nav a': 3}),
        ('directory', '/directory', 'directory', '.units a', {'.units a': 3}),
        ('multiple-columns', '/multiple-columns', 'list', '#aid li', {'#aid li': 2, '#teaching li': 2}),
        ('page-one', '/page-one', 'list', '.notices li', {'.notices li': 2, 'a[rel="next"]': 1}),
        ('page-two', '/page-two', 'list', '.notices li', {'.notices li': 2, 'a[rel="next"]': 0}),
        ('hidden-first-navigation', '/hidden-first-navigation', 'directory', 'a[href]', {'a[href]': 2}),
        ('missing', '/missing', 'directory', '', {}),
    ]
    expected_entries = {
        'root': {'nav a': [('院系设置', '/directory'), ('学生服务', '/multiple-columns'), ('公告入口', '/page-one')]},
        'directory': {'.units a': [('理学院', '/schools/science'), ('工学院', '/schools/engineering'), ('人文学院', '/schools/humanities')]},
        'multiple-columns': {'#' + key + ' a': [(f'{label}第{n}项公开安排通知', f'/{key}/notice/{n}')
            for n in (1, 2)] for key, label in (('aid', '资助公告'), ('teaching', '教学公告'))},
        'page-one': {'.notices a': [('关于学期课程安排的通知', '/notice/1'), ('关于奖助申请安排的通知', '/notice/2')],
                     'a[rel="next"]': [('下一页', '/page-two')]},
        'page-two': {'.notices a': [('关于交流报名安排的通知', '/notice/3'), ('关于学生培养安排的通知', '/notice/4')]},
        'missing': {},
        'hidden-first-navigation': {'a[href]': [('隐藏菜单入口', '/schools/science'), ('教学机构', '/directory')]},
    }
    documents = {
        '/': '<title>测试大学</title><nav><a href="/directory">院系设置</a><a href="/multiple-columns">学生服务</a><a href="/page-one">公告入口</a></nav>',
        '/directory': '''<title>教学机构</title><main aria-busy="true"></main><script>
            fetch('/api/units').then(r=>r.text()).then(s=>setTimeout(()=>{
              document.querySelector('main').innerHTML=s;document.querySelector('main').setAttribute('aria-busy','false');},250));
            fetch('https://example.invalid/fixture-egress-must-be-blocked').catch(()=>{});</script>''',
        '/multiple-columns': '<title>学生与教学服务</title>' + ''.join(
            f'<section id="{key}"><h2>{label}</h2><ul>' + ''.join(
                f'<li><a href="/{key}/notice/{n}">{label}第{n}项公开安排通知</a><time>2026-09-23</time></li>'
                for n in (1, 2)) + '</ul></section>' for key, label in (('aid', '资助公告'), ('teaching', '教学公告'))),
        '/page-one': '<title>通知公告</title><ul class="notices"><li><a href="/notice/1">关于学期课程安排的通知</a></li><li><a href="/notice/2">关于奖助申请安排的通知</a></li></ul><a rel="next" href="/page-two">下一页</a>',
        '/page-two': '<title>通知公告第二页</title><ul class="notices"><li><a href="/notice/3">关于交流报名安排的通知</a></li><li><a href="/notice/4">关于学生培养安排的通知</a></li></ul>',
        '/missing': '<title>404 Not Found</title><p>Not Found</p>',
        '/hidden-first-navigation': '<title>大学导航</title><a hidden href="/schools/science">隐藏菜单入口</a>'
                                    '<main><a href="/directory">教学机构</a></main>',
        '/api/units': '<ul class="units"><li><a href="/schools/science">理学院</a></li><li><a href="/schools/engineering">工学院</a></li><li><a href="/schools/humanities">人文学院</a></li></ul>',
    }
    async def serve(request):
        calls.append(request.path)
        return web.Response(text=documents.get(request.path, 'Not found'),
            content_type='text/html', status=404 if request.path == '/missing' or request.path not in documents else 200)
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', serve)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url('')).rstrip('/')
    validate = fixture_validator(base)
    rows = []
    try:
        for index, (label, path, purpose, ready, expected) in enumerate(cases):
            request = FetchRequest(base + path, purpose=purpose, request_id=label,
                                   readiness_selector=ready, timeout_seconds=args.timeout)
            # Alternate the two cold-session orders. Six local cases do not
            # establish a general speed advantage or an online coverage rate.
            engines = ('current-browser', 'crawl4ai') if index % 2 == 0 else ('crawl4ai', 'current-browser')
            for engine in engines:
                started, offset = time.monotonic(), len(calls)
                if engine == 'crawl4ai':
                    transport = Crawl4AIFixtureTransport(fixture_origin=base,
                        base_directory=out / 'crawl4ai-runtime', browser_channel=args.channel)
                    result = await transport.fetch(request)
                    diagnostics = transport.last_diagnostics
                else:
                    diagnostics = {}
                    async with async_playwright() as playwright:
                        async def launcher(headed, env):
                            return await playwright.chromium.launch(channel=args.channel,
                                headless=True, chromium_sandbox=True)
                        async def only_fixture(url):
                            validate(url)
                        runtime = BrowserRuntime(validator=only_fixture, launcher=launcher)
                        try:
                            await runtime.submit(request.to_dict())
                            await runtime.executions[label].task
                            execution = runtime.get_execution(label)
                            result = (classify_result(request, FetchResult.from_dict(execution['result']))
                                      if execution.get('result') else FetchResult(request.url, transport='browser',
                                      outcome='unavailable', error_code=execution.get('error', {}).get('code', 'fixture_failed')))
                        finally:
                            await runtime.close()
                row = dict(summary(request, result, expected), engine=engine, label=label,
                    engine_diagnostics=diagnostics,
                    elapsed_with_cold_start_and_cleanup_seconds=round(time.monotonic() - started, 4),
                    fixture_http_requests=calls[offset:])
                regions, identity_checks = entry_regions(result.html, expected_entries[label])
                row.update(entry_regions=regions, entry_identity_checks=identity_checks)
                row['expected_verdict'] = (not result.ok and result.status == 404) if label == 'missing' else result.ok
                rows.append(row)
                (out / (engine + '-' + label + '.html')).write_text(result.html, encoding='utf-8')
                dump(out / 'progress.json', rows)
                print(json.dumps({key: row[key] for key in ('engine', 'label', 'status', 'outcome',
                    'error_code', 'expected_structure_present', 'expected_verdict')}, ensure_ascii=False), flush=True)
                await asyncio.sleep(1)
        return {'mode': 'real_system_edge_loopback_fixtures', 'rows': rows,
                'all_structure_and_status_checks_passed': all(row['expected_structure_present']
                    and row['expected_verdict'] and all(row['entry_identity_checks'].values()) for row in rows),
                'actual_fixture_http_requests': len(calls), 'fixture_request_paths': calls,
                'new_website_reads': 0, 'actual_model_requests': 0,
                'scope': 'homepage_navigation_academic_directory_async_menu_multicolumn_entries_pagination',
                'article_pipeline_changed_or_exercised': False,
                'source_installation_or_full_school_acceptance': False,
                'cold_session_timing_not_a_general_speed_benchmark': True}
    finally:
        await server.close()


async def public(args, out):
    """Pair source-structure reads; never instantiate an AI profile or worker."""
    import logging
    import secrets
    import threading
    from werkzeug.serving import make_server
    from bs4 import BeautifulSoup
    from backend import create_app
    from backend.database.db import db
    from backend.browser_service.runtime import BrowserRuntime
    from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
    from backend.scraper.acquisition.crawl4ai_transport import Crawl4AIPublicDiagnosticTransport
    from backend.services.source_inventory import resolve_page_link
    from playwright.async_api import async_playwright
    cases = json.loads(args.public_manifest.read_text('utf-8'))
    if not isinstance(cases, list) or not cases:
        raise ValueError('An explicit non-empty public observation manifest is required')
    os.environ['WATCHER_BROWSER_TOKEN'] = secrets.token_hex(32)
    app = create_app({'TESTING': True, 'SECRET_KEY': secrets.token_hex(32),
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str((out / 'isolated-data' / 'broker.db').resolve()),
        'BROWSER_SERVICE_TOKEN': os.environ['WATCHER_BROWSER_TOKEN']})
    with app.app_context():
        db.create_all()
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    os.environ['WATCHER_ORIGIN_BROKER_URL'] = f'http://127.0.0.1:{server.server_port}/internal/browser-origin'
    transport = Crawl4AIPublicDiagnosticTransport(base_directory=out / 'crawl4ai-runtime',
        allow_public_diagnostics=True, browser_channel=args.channel)
    rows = []
    try:
        for index, case in enumerate(cases):
            if case.get('purpose', 'directory') not in ('directory', 'list'):
                raise ValueError('Article extraction is outside this comparison')
            expected = case.get('expected_structure', {})
            label = str(index)
            engines = ('current-browser', 'crawl4ai') if index % 2 == 0 else ('crawl4ai', 'current-browser')
            for engine in engines:
                request = FetchRequest(case['url'], purpose=case.get('purpose', 'directory'),
                    readiness_selector=case.get('readiness_selector', ''),
                    timeout_seconds=args.timeout, request_id=engine + ':' + label)
                start = time.monotonic()
                if engine == 'crawl4ai':
                    result = await transport.fetch(request)
                    diagnostics = dict(transport.last_diagnostics)
                else:
                    diagnostics = {'sandbox_enabled': True, 'shared_origin_broker': True}
                    async with async_playwright() as driver:
                        async def launch(headed, env):
                            return await driver.chromium.launch(channel=args.channel, headless=True,
                                chromium_sandbox=True)
                        runtime = BrowserRuntime(launcher=launch)
                        try:
                            await runtime.submit(request.to_dict())
                            await runtime.executions[request.request_id].task
                            observed = runtime.get_execution(request.request_id)
                            result = (classify_result(request, FetchResult.from_dict(observed['result']))
                                if observed.get('result') else FetchResult(request.url, transport='browser',
                                outcome='unavailable', error_code=observed.get('error', {}).get('code', 'read_failed')))
                        finally:
                            await runtime.close()
                row = dict(summary(request, result, expected), engine=engine, case_index=index,
                    name=case.get('name', ''), kind=case.get('kind', ''), engine_diagnostics=diagnostics,
                    elapsed_with_cold_start_and_cleanup_seconds=round(time.monotonic() - start, 4))
                links = []
                for anchor in BeautifulSoup(result.html, 'lxml').select('a[href]'):
                    try:
                        target = resolve_page_link(result.final_url, anchor['href'])
                    except ValueError:
                        continue
                    if target:
                        links.append({'url': target, 'text': anchor.get_text(' ', strip=True)})
                row['observed_links'] = links
                actual = {link['url'] for link in links}
                row['expected_observed_links'] = {url: url in actual for url in case.get('expected_links', [])}
                rows.append(row)
                (out / (engine + '-' + label + '.html')).write_text(result.html, encoding='utf-8')
                dump(out / 'progress.json', rows)
                print(json.dumps({key: row[key] for key in ('engine', 'case_index', 'name', 'status',
                    'outcome', 'error_code', 'elapsed_with_cold_start_and_cleanup_seconds')}, ensure_ascii=False), flush=True)
                await asyncio.sleep(1)
        return {'mode': 'real_public_source_structure', 'rows': rows,
                'website_page_attempts': len(rows), 'actual_model_requests': 0,
                'user_profile_or_database_used': False, 'article_pipeline_changed_or_exercised': False,
                'source_installation_or_full_school_acceptance': False,
                'same_budget_source_acceptance_complete': False}
    finally:
        await asyncio.to_thread(server.shutdown)
        thread.join(timeout=5)
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('retained', 'fixtures', 'public'), default='retained')
    parser.add_argument('--public-manifest', type=Path)
    parser.add_argument('--retained-directory', type=Path, default=ROOT / '.local/crawl4ai-evaluation')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--channel', choices=('msedge', 'chrome'), default='msedge')
    parser.add_argument('--timeout', type=float, default=15)
    args = parser.parse_args()
    if args.mode == 'public' and not args.public_manifest:
        parser.error('--public-manifest is required for explicit public diagnostics')
    if not 0 < args.timeout <= 45:
        parser.error('Use a bounded per-fixture timeout between 0 and 45 seconds')
    out = args.output or ROOT / '.local/crawl4ai-plan-eval' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out.mkdir(parents=True, exist_ok=False)
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
        LITELLM_LOCAL_MODEL_COST_MAP='true', WATCHER_DATA_DIR=str(out / 'isolated-data'),
        WATCHER_ENV_FILE=str(out / 'no-env'), WATCHER_ORIGIN_BROKER_URL='',
        CRAWL4_AI_BASE_DIRECTORY=str(out / 'crawl4ai-runtime'))
    result = (asyncio.run(public(args, out)) if args.mode == 'public' else
              asyncio.run(fixtures(args, out)) if args.mode == 'fixtures' else retained(args, out))
    result['recorded_at_utc'] = datetime.now(timezone.utc).isoformat()
    dump(out / 'results.json', result)
    print(str(out / 'results.json'))
    return 0 if result.get('all_structure_and_status_checks_passed', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
