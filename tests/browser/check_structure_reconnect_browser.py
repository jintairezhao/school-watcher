"""Exercise real localhost HTTP and browser offline recovery on disposable data.

The university-like documents below are behavioral fixtures, not evidence of
real ownership, crawling quality, or threshold performance. No website/model is
contacted and no production profile or installed application is opened.
"""
from contextlib import ExitStack
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import argparse
import hashlib
import json
import socket
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_structure_snapshot import StructureSnapshotTests
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department, Subscription
from backend.database.structure_version_models import StructureChange
from backend.services.structure_snapshot import _transport, _transport_lock
from backend.services.structure_versions import current_version, reset_epoch
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import WSGIRequestHandler, make_server


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def check(output, offline_seconds=80):
    output = Path(output).resolve()
    assert output.is_relative_to(ROOT / '.local') and not output.exists()
    assert 65 <= offline_seconds <= 180, 'Sustained outage must cross the 30s backoff cap'
    output.mkdir(parents=True)
    fixture = StructureSnapshotTests()
    fixture.setUp()
    server = thread = None
    trace, results, errors, external, writes = [], [], [], [], []
    lock = Lock()
    fault = {'mismatched_delta': False}
    native_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1', 'localhost'):
            return native_connect(sock, address)
        raise AssertionError('Offline-only test attempted a non-loopback connection')

    try:
        source = fixture.source('持续断线测试栏目')
        extra = fixture.source('第二个测试栏目', fixture.school.url + '/other')
        source_id, extra_id, school_id = source.id, extra.id, fixture.school.id
        directory = fixture.school.url + '/departments'
        pages = [fixture.document(fixture.school.url, directory, label='测试官网'),
                 fixture.document(directory, source.list_url, extra.list_url, label='测试目录'),
                 fixture.document(source.list_url, label='测试栏目页面'),
                 fixture.document(extra.list_url, label='第二测试页面')]
        pages[0]['kind'], pages[1]['kind'] = 'root', 'directory'
        fixture.fixture.publish(pages)
        personal_before = dict(
            subscriptions=[(r.id, r.user_id, r.school_id, r.department_ids) for r in Subscription.query.order_by(Subscription.id)],
            announcements=[(r.id, r.title, r.url) for r in Announcement.query.order_by(Announcement.id)],
            tasks=BackgroundTask.query.count())
        app = fixture.app

        @app.after_request
        def observe(response):
            from flask import request
            if request.method != 'GET':
                writes.append(dict(method=request.method, path=request.path))
            if request.path == fixture.endpoint:
                payload = response.get_json(silent=True) or {}
                with lock:
                    injected = fault['mismatched_delta'] and payload.get('update_mode') == 'delta'
                    if injected:
                        fault['mismatched_delta'] = False
                        payload['base_token'] = 'injected-missing-client-base'
                        response.set_data(json.dumps(payload, ensure_ascii=False))
                    trace.append(dict(at=monotonic(), status=response.status_code,
                        since=request.args.get('since'), mode=payload.get('update_mode'),
                        version=payload.get('structure_version'), token=payload.get('structure_token'),
                        injected_base_mismatch=injected))
            return response

        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietHandler)
        origin = f'http://127.0.0.1:{server.server_port}'
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cookie = fixture.client.get_cookie(app.config['SESSION_COOKIE_NAME'])
        assert cookie
        with ExitStack() as guards:
            guards.enter_context(patch('socket.socket.connect', new=local_connect))
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='msedge', headless=True,
                    args=['--enable-webgl', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                try:
                    for width, locale in ((1440, 'en'), (390, 'zh-CN'), (1440, 'zh-CN'), (390, 'en')):
                        context = browser.new_context(viewport={'width': width, 'height': 1000}, reduced_motion='reduce')
                        context.add_cookies([dict(name=cookie.key, value=cookie.value, url=origin)])
                        def guard_browser(route):
                            request = route.request
                            if urlsplit(request.url).netloc != urlsplit(origin).netloc:
                                external.append(request.url)
                                return route.abort()
                            if request.method != 'GET':
                                writes.append(dict(method=request.method, path=urlsplit(request.url).path))
                                return route.abort()
                            route.continue_()
                        context.route('**/*', guard_browser)
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        failures = []
                        page.on('requestfailed', lambda request: failures.append(dict(
                            path=urlsplit(request.url).path, at=monotonic(), error=request.failure)))
                        page.goto(origin + fixture.fixture.page)
                        expect(page.locator('#atlasStage')).to_have_attribute('data-renderer', 'ready')
                        page.wait_for_function('discoveryMonitor.monitorSnapshot?.source_count===2')
                        page.evaluate('(locale)=>I18n.setLocale(locale)', locale)
                        page.locator('#atlasOptions > summary').click()
                        page.locator('#atlasNodeLabels').uncheck()
                        page.locator('#atlasOptions > summary').click()
                        # The collapsible inspector is an immersive-mode control;
                        # the regular page intentionally has a persistent column.
                        page.locator('#atlasFullscreen').click()
                        page.evaluate('(id)=>discoveryMonitor.discoveryAtlas.select(id)', f'source:{source_id}')
                        expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                        before = page.evaluate('''()=>({token:discoveryMonitor.monitorSnapshot.structure_token,
                            version:discoveryMonitor.monitorSnapshot.structure_version,
                            nodes:discoveryMonitor.monitorSnapshot.nodes,
                            relations:discoveryMonitor.monitorSnapshot.relations})''')
                        began = monotonic()
                        lost_message = page.evaluate("uiT('连接中断，保留上次数据')")
                        context.set_offline(True)
                        expect(page.locator('#monitorConnection')).to_contain_text(
                            lost_message, timeout=20000)
                        assert page.evaluate('discoveryMonitor.monitorSnapshot.structure_token') == before['token']
                        assert page.evaluate('discoveryMonitor.discoveryAtlas.current.id') == f'source:{source_id}'
                        expect(page.locator('#atlasDetailPanel')).to_be_hidden()

                        # The first run waits real wall time through repeated automatic
                        # failures; other language/width runs use a shorter outage.
                        sustained = width == 1440 and locale == 'en'
                        new_name = f'恢复后的测试栏目 {width} {locale}'
                        source = db.session.get(Department, source_id)
                        source.name = '断线中间版本不应作为最终结果'
                        db.session.commit()
                        source.name = new_name
                        db.session.commit()
                        committed = current_version(school_id)
                        # A restarted process has no exact old payload in its cache.
                        with _transport_lock:
                            _transport.clear()
                        while sustained and monotonic() - began < offline_seconds:
                            page.wait_for_timeout(min(1000, (offline_seconds - (monotonic() - began)) * 1000))
                            assert page.evaluate('discoveryMonitor.monitorSnapshot.structure_token') == before['token']
                        offline_duration = monotonic() - began
                        attempt_count = sum(r['path'] == fixture.endpoint for r in failures)
                        assert attempt_count >= (4 if sustained else 1), failures
                        context.set_offline(False)
                        page.wait_for_function('''({id,name})=>discoveryMonitor.monitorSnapshot?.nodes.some(
                            node=>node.id===id&&node.label===name)''',
                            arg=dict(id=f'source:{source_id}', name=new_name), timeout=40000)
                        restored = page.evaluate('discoveryMonitor.monitorSnapshot')
                        assert restored['structure_version'] == committed
                        assert restored['update_mode'] == 'snapshot'
                        assert page.evaluate('discoveryMonitor.discoveryAtlas.current.id') == f'source:{source_id}'
                        expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                        assert not page.locator('#atlasNodeLabels').is_checked()
                        assert page.locator('.atlas-label:not([hidden])').count() == 0
                        assert len({n['id'] for n in restored['nodes']}) == len(restored['nodes'])
                        assert len({r['id'] for r in restored['relations']}) == len(restored['relations'])
                        assert restored['source_count'] == 2
                        expect(page.locator('#monitorError')).to_be_hidden()
                        restored_connection = page.locator('#monitorConnection').inner_text()
                        assert lost_message not in restored_connection
                        stages = [dict(stage='cache-gap-recovery', mode=restored['update_mode'], version=committed)]

                        if sustained:
                            # Old browser token must be fenced after a real epoch change.
                            reset_epoch(school_id, 'isolated browser restore-boundary check')
                            db.session.commit()
                            epoch = current_version(school_id)
                            page.wait_for_function('epoch=>discoveryMonitor.monitorSnapshot?.structure_version.epoch===epoch',
                                arg=epoch['epoch'], timeout=15000)
                            assert page.evaluate('discoveryMonitor.monitorSnapshot.update_mode') == 'snapshot'
                            stages.append(dict(stage='epoch-reset', mode='snapshot', version=epoch))
                            # Removing a journal row simulates an actual missed durable
                            # update; a surviving process cache cannot justify a delta.
                            source.name = '事务日志缺口后的最终栏目'
                            db.session.commit()
                            gap_version = current_version(school_id)
                            StructureChange.query.filter_by(school_id=school_id, epoch=gap_version['epoch'],
                                revision=gap_version['revision']).delete()
                            db.session.commit()
                            page.wait_for_function('v=>discoveryMonitor.monitorSnapshot?.structure_version.revision===v',
                                arg=gap_version['revision'], timeout=15000)
                            assert page.evaluate('discoveryMonitor.monitorSnapshot.update_mode') == 'snapshot'
                            stages.append(dict(stage='journal-gap', mode='snapshot', version=gap_version))
                            # This last case injects only a protocol fault. It does not
                            # certify a real outage or university fact.
                            with lock:
                                fault['mismatched_delta'] = True
                                trace_start = len(trace)
                            page.locator('#monitorRefresh').evaluate('(button)=>button.click()')
                            page.wait_for_function('''()=>discoveryMonitor.monitorSnapshot?.update_mode==='snapshot'
                                &&!document.querySelector('#monitorRefresh').disabled''')
                            with lock:
                                recent = list(trace[trace_start:])
                            assert any(r['injected_base_mismatch'] for r in recent), recent
                            assert recent[-1]['mode'] == 'snapshot' and recent[-1]['since'] is None, recent
                            stages.append(dict(stage='injected-mismatched-delta', requests=recent))
                        # Compare actual HTTP-loaded WebGL data to a fresh actual API read.
                        final = fixture.get()
                        rendered = page.evaluate('discoveryMonitor.monitorSnapshot')
                        assert rendered['nodes'] == final['nodes'] and rendered['relations'] == final['relations']
                        assert rendered['structure_version'] == final['structure_version']
                        assert page.evaluate('[...discoveryMonitor.discoveryAtlas.model.actual.keys()].sort()') == sorted(n['id'] for n in final['nodes'])
                        projected=page.evaluate('discoveryMonitor.discoveryAtlas.nodes.map(n=>n.id)')
                        assert len(projected)==len(set(projected)) and set(projected)<=set(n['id'] for n in final['nodes'])
                        assert page.evaluate('discoveryMonitor.discoveryAtlas.current.id') == f'source:{source_id}'
                        expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                        page.screenshot(path=str(output / f'reconnected-{width}-{locale}.png'), full_page=True)
                        results.append(dict(width=width, locale=locale, sustained=sustained,
                            offline_seconds=round(offline_duration, 3), failed_snapshot_requests=attempt_count,
                            retained_last_snapshot=True, recovered_without_manual_refresh=True,
                            source_count=2, selection_preserved=True, details_remain_closed=True,
                            labels_remain_off=True, duplicate_nodes=0, duplicate_relations=0, stages=stages))
                        context.close()
                finally:
                    browser.close()
        personal_after = dict(
            subscriptions=[(r.id, r.user_id, r.school_id, r.department_ids) for r in Subscription.query.order_by(Subscription.id)],
            announcements=[(r.id, r.title, r.url) for r in Announcement.query.order_by(Announcement.id)],
            tasks=BackgroundTask.query.count())
        assert personal_before == personal_after
        assert not writes and not external and not errors, (writes, external, errors)
        assert Department.query.filter(Department.id.in_([source_id, extra_id])).count() == 2
        result = dict(cases=results, requests=trace, errors=errors, external_requests=external,
            browser_writes=writes, personal_fixture_data_unchanged=True,
            actual_localhost_http=True, accelerated_clock=False, official_requests=0, model_calls=0,
            real_university_fact_acceptance=False, production_database_access='none')
        (output / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        (output / 'artifact-hashes.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest()
            for p in output.iterdir() if p.is_file() and p.name != 'artifact-hashes.json'}, indent=2) + '\n', 'utf-8')
        print(json.dumps(dict(cases=len(results), sustained_offline_seconds=results[0]['offline_seconds'],
            requests=len(trace), errors=errors, writes=writes), ensure_ascii=False))
    except BaseException as error:
        (output / 'failure.json').write_text(json.dumps(dict(error=repr(error), cases=results,
            requests=trace, errors=errors, external_requests=external, browser_writes=writes),
            ensure_ascii=False, indent=2) + '\n', 'utf-8')
        raise
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=5)
        fixture.tearDown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--offline-seconds', type=int, default=80)
    args = parser.parse_args()
    check(args.output, args.offline_seconds)
