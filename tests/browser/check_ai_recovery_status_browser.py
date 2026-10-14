"""Verified model recovery updates current status while cumulative usage stays put."""
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from backend.ai.models import AIProfile, AIBinding, AIBudget, AISchoolRound
from backend.ai.providers import ProviderResult
from backend.ai.runtime import test_connection
from backend.database.db import db
from backend.database.models import User, WorkerHeartbeat
from test_discovery_ai_progress import DiscoveryAIProgressTests
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = DiscoveryAIProgressTests(); fixture.setUp()
    server = None
    output = ROOT / '.local/ai-recovery-status-check'; output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    try:
        profile = AIProfile(name='隔离测试配置', provider='deepseek', region='default', model='fixture',
            encrypted_key='isolated-fixture', version=1, tested_version=1, enabled=True, last_test_code='ok')
        db.session.add(profile); db.session.flush()
        db.session.add(AIBinding(purpose='directory', profile_id=profile.id))
        key = f'school-discovery:{fixture.school.id}:fixture'
        ledger = AIBudget(key=key, used_requests=5, used_tokens=8239, reserved_tokens=11068)
        db.session.add_all([AISchoolRound(school_id=fixture.school.id, budget_key=key), ledger])
        db.session.add(WorkerHeartbeat(worker_id='isolated-fixture', roles=['directory']))
        user = User.query.filter_by(username='reader').one(); user.role = 'admin'
        job = fixture.waiting_ai('ai_connection', 'provider_connection_paused')
        job.checkpoint = {'route_cursor': 7, 'ai_connection_binding': {
            'id': profile.id, 'version': 1, 'provider': 'deepseek', 'model': 'fixture'},
            'discovery_progress': {'ai_state': 'failed', 'ai_error_code': 'provider_connection_paused'}}
        db.session.commit()
        app = fixture.fixture.app
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': user.id})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        with patch('backend.ai.providers.complete', side_effect=AssertionError('No real provider call')), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'))
            try:
                context = browser.new_context(reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base + '/') else r.abort())
                page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
                page.set_viewport_size({'width': 1280, 'height': 850})
                page.goto(base + f'/subscriptions/{fixture.school.id}')
                page.locator('.discovery-details > summary').click()
                expect(page.locator('#discoveryAI')).to_contain_text('连续连接失败')
                expect(page.locator('#discoveryAI')).to_contain_text('重新测试')
                expect(page.locator('#discoveryAIUsage')).to_contain_text('本轮累计调用 5 次')
                expect(page.locator('#discoveryAIUsage')).to_contain_text('已确认用量 8,239 Token')
                expect(page.locator('#discoveryAIUsage')).to_contain_text('预留 11,068 Token')
                checks.append('Paused exploration explains its cause separately from cumulative calls and pending usage')

                # Exercise the actual test-and-resume transaction against a
                # controlled provider response, preserving the school's budget.
                response = ProviderResult('{"ok":true}', 'stop', {'known': True, 'input_tokens': 49,
                    'output_tokens': 5, 'total_tokens': 54}, 'fixture-test')
                with patch('backend.ai.runtime.credential_for', return_value='isolated-fixture'), \
                     patch('backend.ai.providers.complete', return_value=response) as provider:
                    assert test_connection(profile.id)['tested'] is True
                provider.assert_called_once()
                page.locator('#discoveryRefresh').click()
                expect(page.locator('#discoveryAI')).to_contain_text('已安排')
                expect(page.locator('#discoveryAI')).not_to_contain_text('连接失败')
                expect(page.locator('#discoveryAIUsage')).to_contain_text('本轮累计调用 5 次')
                checks.append('Successful structured test resumes the same task and removes its old paused status')

                for width, theme in ((1280, 'dark'), (390, 'light')):
                    page.set_viewport_size({'width': width, 'height': 850})
                    page.reload(); page.locator('.discovery-details > summary').click()
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    expect(page.locator('#discoveryAI')).to_contain_text('已安排')
                    expect(page.locator('#discoveryAIUsage')).to_contain_text('待确认')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                    page.locator('#discoveryStatus').screenshot(path=str(output / f'recovered-{width}.png'))
                checks.append('Reload, desktop, mobile and both themes retain clear current status and cumulative usage')

                # Replay a checkpoint already resumed by the previous app version.
                db.session.refresh(job)
                job.checkpoint = dict(job.checkpoint, discovery_progress={
                    'ai_state': 'failed', 'ai_error_code': 'provider_connection_paused'})
                db.session.commit()
                page.locator('#discoveryRefresh').click()
                expect(page.locator('#discoveryAI')).to_contain_text('已安排')
                checks.append('Existing resumed checkpoints are projected correctly without an extra test or replay')

                # A genuinely new pause must still be reported after recovery.
                job.state, job.phase, job.error_code = 'waiting', 'ai_connection', 'provider_connection_paused'
                db.session.commit()
                page.locator('#discoveryRefresh').click()
                expect(page.locator('#discoveryAI')).to_contain_text('连续连接失败')
                checks.append('A new actual pause is visible rather than hidden by prior recovery')
            finally:
                browser.close()
        db.session.refresh(job); db.session.refresh(ledger)
        assert job.checkpoint['route_cursor'] == 7
        assert (ledger.used_requests, ledger.used_tokens, ledger.reserved_tokens) == (5, 8239, 11068)
        assert not errors, errors
        report = {'checks': checks, 'page_errors': errors, 'school_budget_and_cursor_preserved': True,
            'actual_model_requests': 0, 'actual_official_requests': 0, 'controlled_connection_tests': 1}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf8')
        print(json.dumps(report, ensure_ascii=True))
    finally:
        if server: server.shutdown(); server.server_close()
        fixture.tearDown()


if __name__ == '__main__': check()
