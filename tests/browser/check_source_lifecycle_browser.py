"""Source-state controls and monitor convergence on disposable same-page data."""
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
import faulthandler
import hashlib
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from playwright.sync_api import sync_playwright, expect
from sqlalchemy.exc import OperationalError
from tests.test_shared_page_mutations import SharedPageMutationTests
from backend.database.db import db
from backend.database.models import User
from backend.services.localization import stage_preferences
from backend.services.structure_versions import current_version


def check(output):
    output = Path(output).resolve()
    assert output.is_relative_to(ROOT/'.local')
    output.mkdir(parents=True, exist_ok=True)
    assert not (output/'browser-acceptance.json').exists(), 'Reuse completed browser evidence'
    sha = lambda p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
    baseline = json.loads((output.parent/'before.json').read_text(encoding='utf-8-sig'))
    assert all(sha(p) == h for p, h in baseline['protected_fingerprints'].items())
    fingerprints = {p: sha(p) for p in ['backend/routes/schools.py', 'backend/routes/source_lifecycle.py',
        'backend/services/source_lifecycle.py', 'frontend/templates/_admin_schools.html',
        'frontend/static/css/style.css', 'frontend/static/i18n/en.json']}
    rows = []
    print('Starting local Edge browser', flush=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        try:
            for locale in ('zh-CN', 'en'):
                if os.environ.get('WATCHER_TEST_LOCALE') and locale != os.environ['WATCHER_TEST_LOCALE']:
                    continue
                for width in (1280, 390):
                    if os.environ.get('WATCHER_TEST_WIDTH') and str(width) != os.environ['WATCHER_TEST_WIDTH']:
                        continue
                    fixture = SharedPageMutationTests()
                    fixture.setUp()
                    context = None
                    try:
                        school = fixture.f.school
                        stage_preferences(User.query.first().id, {'locale': locale})
                        db.session.commit()
                        initial = current_version(school.id)
                        saved = fixture.retained()
                        source_id = f'source:{fixture.first.id}'
                        sibling_id = f'source:{fixture.second.id}'
                        context = browser.new_context(viewport={'width': width, 'height': 960}, reduced_motion='reduce')
                        errors, writes, graph_modes = [], [], []
                        fail_once = True

                        def serve(route):
                            nonlocal fail_once
                            request = route.request
                            parsed = urlsplit(request.url)
                            assert parsed.hostname == 'localhost', 'No external request: ' + request.url
                            path = parsed.path + ('?' + parsed.query if parsed.query else '')
                            if path.startswith('/_desktop/'):
                                return route.fulfill(json={'phase': 'ready'})
                            headers = {k: v for k, v in request.headers.items()
                                       if k.lower() in ('content-type', 'x-csrf-token')}
                            lifecycle = parsed.path == f'/api/departments/{fixture.first.id}/lifecycle'
                            if request.method != 'GET':
                                assert lifecycle or parsed.path == '/api/preferences/localization', path
                            if lifecycle and fail_once:
                                fail_once = False
                                with patch('backend.routes.source_lifecycle.db.session.commit',
                                        side_effect=OperationalError('injected lifecycle commit failure', {}, None)):
                                    response = fixture.client.open(path, method=request.method,
                                        data=request.post_data, headers=headers)
                            else:
                                response = fixture.client.open(path, method=request.method,
                                    data=request.post_data, headers=headers)
                            if lifecycle:
                                writes.append(dict(status=response.status_code,
                                    state=request.post_data_json['state'], csrf=bool(headers.get('x-csrf-token'))))
                            if parsed.path.endswith('/structure-snapshot') and response.status_code == 200:
                                graph_modes.append(response.json['update_mode'])
                            route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

                        context.route('**/*', serve)
                        admin = context.new_page()
                        monitor = context.new_page()
                        for page in (admin, monitor):
                            page.on('pageerror', lambda error: errors.append(str(error)))
                        admin.goto('http://localhost/admin#schools')
                        admin.locator(f'[onclick="manageDept({school.id})"]').click()
                        expect(admin.locator('#deptList [data-dept-state]')).to_have_count(2)
                        card = admin.locator('#deptList .manage-card').filter(has=admin.locator('h4', has_text=fixture.first.name))
                        control = card.locator('[data-dept-state]')
                        status = card.locator('[data-dept-state-status]')
                        expect(control).to_have_value('active')
                        expect(card.locator('[data-dept-action="delete"]')).to_have_text(
                            '永久删除' if locale == 'zh-CN' else 'Delete permanently')
                        monitor.goto(f'http://localhost/subscriptions/{school.id}/discovery-monitor')
                        expect(monitor.locator('#atlasStage')).to_have_attribute('data-renderer', 'ready')
                        monitor.locator('#monitorAuto').uncheck()
                        monitor.wait_for_function('ids => ids.every(id => discoveryMonitor.monitorSnapshot?.nodes.some(n => n.id === id))',
                                                  arg=[source_id, sibling_id])
                        initial_token = monitor.evaluate('discoveryMonitor.monitorSnapshot.structure_token')

                        # The first write actually rolls back at the native commit.
                        control.select_option('disabled')
                        expect(status).to_have_text('来源状态未修改，历史信息仍保留。请稍后重试。' if locale == 'zh-CN'
                            else 'The source state was not changed. Past information is kept. Try again shortly.')
                        expect(control).to_have_value('active')
                        expect(control).to_be_enabled()
                        assert current_version(school.id) == initial
                        assert fixture.retained() == saved
                        monitor.locator('#monitorRefresh').click()
                        monitor.wait_for_function('token => discoveryMonitor.monitorSnapshot.structure_token === token', arg=initial_token)

                        versions = []
                        for state in ('disabled', 'removed', 'active'):
                            control.select_option(state)
                            expect(status).to_have_text('状态已保存' if locale == 'zh-CN' else 'State saved')
                            expect(control).to_have_value(state)
                            version = current_version(school.id)
                            assert version == dict(initial, revision=initial['revision'] + len(versions) + 1)
                            versions.append(dict(state=state, version=version))
                            assert fixture.retained() == saved
                            fresh = fixture.get()
                            source_ids = [n['id'] for n in fresh['nodes'] if n['entity_type'] == 'source']
                            assert sibling_id in source_ids and (source_id in source_ids) == (state == 'active')
                            monitor.locator('#monitorRefresh').click()
                            monitor.wait_for_function('expected => discoveryMonitor.monitorSnapshot?.structure_version.revision === expected',
                                                      arg=version['revision'])
                            actual_ids = monitor.evaluate('discoveryMonitor.monitorSnapshot.nodes.map(n => n.id)')
                            assert sibling_id in actual_ids and (source_id in actual_ids) == (state == 'active')
                            assert len(actual_ids) == len(set(actual_ids))
                            assert monitor.evaluate('id => discoveryMonitor.discoveryAtlas.nodes.some(n => n.id === id)', sibling_id)
                            monitor.wait_for_function('expected => discoveryMonitor.discoveryAtlas.nodes.some(n => n.id === expected.id) === expected.visible',
                                                      arg=dict(id=source_id, visible=state == 'active'))
                            if state != 'active':
                                admin.reload()
                                admin.locator(f'[onclick="manageDept({school.id})"]').click()
                                expect(control).to_have_value(state)

                        # Reload reads the persisted state, not a retained selector value.
                        admin.reload()
                        admin.locator(f'[onclick="manageDept({school.id})"]').click()
                        card = admin.locator('#deptList .manage-card').filter(has=admin.locator('h4', has_text=fixture.first.name))
                        expect(card.locator('[data-dept-state]')).to_have_value('active')
                        expect(admin.locator('#deptLifecycleHint')).to_contain_text(
                            '历史通知' if locale == 'zh-CN' else 'Past notices')
                        assert card.locator('[data-dept-state]').evaluate('(node) => node.getBoundingClientRect().width') >= 90
                        assert admin.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                        assert not errors, errors
                        assert len(writes) == 4 and [r['status'] for r in writes] == [500, 200, 200, 200]
                        assert all(r['csrf'] for r in writes)
                        assert 'delta' in graph_modes
                        screenshot = output/f'lifecycle-{locale}-{width}.png'
                        admin.screenshot(path=str(screenshot), full_page=True)
                        rows.append(dict(locale=locale, width=width, failure_rollback_and_retry=True,
                            same_page_sibling_preserved=True, selected_source_identity_preserved=True,
                            monitor_matches_committed_version=True, reload_reads_persisted_state=True,
                            rendered_node_membership_matches_state=True, inactive_reload_reads_persisted_state=True,
                            synthetic_history_unchanged=True, versions=versions, writes=writes,
                            graph_modes=graph_modes, screenshot=screenshot.name, page_errors=errors))
                        print(f'Passed {locale} {width}', flush=True)
                    finally:
                        if context: context.close()
                        fixture.doCleanups()
        finally:
            browser.close()
    assert all(sha(p) == h for p,h in baseline['protected_fingerprints'].items())
    result = dict(completed=True, scenarios=rows, browser='headless Microsoft Edge',
        actual_routes_and_product_scripts=True, requests_intercepted_to_flask_test_client=True,
        live_http_server_or_installed_program_tested=False, fixture_is_synthetic_not_website_ownership_evidence=True,
        product_fingerprints=fingerprints, protected_fingerprints=baseline['protected_fingerprints'],
        browser_script_sha256=sha('tests/browser/check_source_lifecycle_browser.py'),
        website_requests=0, model_calls=0, worker_tasks_executed=0,
        installation_updated=False, public_release=False)
    (output/'browser-acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(completed=True, scenarios=len(rows), external_requests=0, model_calls=0)))


if __name__ == '__main__':
    faulthandler.dump_traceback_later(45, repeat=True)
    try:
        check(sys.argv[1] if len(sys.argv) > 1 else
              ROOT/'.local/structure-facts-20261021/source-lifecycle-interface-20261101/browser')
    finally:
        faulthandler.cancel_dump_traceback_later()
