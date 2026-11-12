"""Selection/details must follow the committed structure, using disposable data.

The saved documents are behavioral fixtures, not university ownership evidence.
Every browser request is served by the Flask test client; no website/model runs.
"""
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
import argparse
import faulthandler
import hashlib
import json
import os
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
NATIVE_CONNECT = socket.socket.connect
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from sqlalchemy.exc import OperationalError


def check(output, expect_stale=False, expect_unavailable_stale=False):
    output = Path(output).resolve()
    assert output.is_relative_to(ROOT / '.local') and not output.exists()
    output.mkdir(parents=True)
    temporary = output / 'temp'
    temporary.mkdir()
    tempfile.tempdir = str(temporary)
    os.environ.update(WATCHER_ENV_FILE=str(temporary / 'no-env'),
        WATCHER_DATA_DIR=str(temporary / 'unused-data'), SECRET_KEY='selection-fixture-only', WATCHER_SEED_ON_START='0')
    from tests.test_shared_page_mutations import SharedPageMutationTests
    from backend.database.db import db
    from backend.services.structure_versions import current_version, reset_epoch
    from backend.services.structure_snapshot import _transport, _transport_lock
    fixture = SharedPageMutationTests()
    fixture.setUp()
    print('Disposable structure fixture ready', flush=True)
    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1', 'localhost'):
            return NATIVE_CONNECT(sock, address)
        raise AssertionError('No external socket connection')
    connection_guard = patch('socket.socket.connect', new=local_connect)
    connection_guard.start()
    browser = None
    try:
        school_id = fixture.f.school.id
        source_id, sibling_id = f'source:{fixture.first.id}', f'source:{fixture.second.id}'
        retained = fixture.retained()
        errors, responses, results = [], [], []
        with sync_playwright() as pw:
            print('Starting local browser', flush=True)
            browser = pw.chromium.launch(channel='msedge', headless=True, chromium_sandbox=True, timeout=20000,
                args=['--enable-webgl', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={'width': 1280, 'height': 960}, reduced_motion='reduce')
            page.set_default_timeout(10000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            def serve(route):
                request, parsed = route.request, urlsplit(route.request.url)
                assert parsed.hostname == 'localhost', 'No external browser request'
                if parsed.path.startswith('/_desktop/'):
                    return route.fulfill(json={'phase': 'ready'})
                assert request.method == 'GET', 'This browser only reads structure'
                response = fixture.client.get(parsed.path + ('?' + parsed.query if parsed.query else ''))
                if parsed.path.endswith('/structure-snapshot') and response.status_code == 200:
                    responses.append(response.json)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

            page.route('**/*', serve)
            page.goto(f'http://localhost/subscriptions/{school_id}/discovery-monitor')
            print('Local monitor loaded', flush=True)
            expect(page.locator('#atlasStage')).to_have_attribute('data-renderer', 'ready')
            page.locator('#monitorAuto').uncheck()
            page.wait_for_function('id => discoveryMonitor.monitorSnapshot?.nodes.some(n => n.id === id)', arg=source_id)

            def refresh(keep_focus=False):
                before = len(responses)
                if keep_focus:
                    page.locator('#monitorRefresh').evaluate('(button) => button.click()')
                else:
                    page.locator('#monitorRefresh').click()
                page.wait_for_function('revision => discoveryMonitor.monitorSnapshot?.structure_version.revision === revision',
                    arg=current_version(school_id)['revision'])
                expect(page.locator('#monitorRefresh')).to_be_enabled()
                assert len(responses) > before
                choices = fixture.client.get(f'/api/subscriptions/{school_id}/discovery')
                assert choices.status_code == 200
                assert choices.json['structure_version'] == responses[-1]['structure_version']
                return choices.json

            def unavailable_and_restore():
                page.locator('#atlasListPanel').evaluate('(node) => node.open = true')
                page.locator(f'.monitor-node[data-node-id="{sibling_id}"]').click()
                page.locator('#atlasListPanel').evaluate('(node) => node.open = false')
                fixture.f.school.enabled = False
                db.session.commit()
                page.locator('#monitorRefresh').click()
                expect(page.locator('#monitorError')).to_be_visible()
                page.wait_for_function('discoveryMonitor.monitorSnapshot === null')
                stale = page.locator('#discoveryMonitor').get_attribute('data-selected-id') is not None
                if expect_unavailable_stale:
                    assert stale, 'Unavailable-view selection defect did not reproduce'
                    return dict(reproduced=True, actual_school_disabled_response=404,
                        snapshot_cleared=True, selected_identity_still_retained=True)
                assert not stale, 'Unavailable view retained its selected identity'
                expect(page.locator('#monitorDetail h3')).to_have_count(0)
                fixture.f.school.enabled = True
                db.session.commit()
                refresh()
                assert responses[-1]['update_mode'] == 'snapshot'
                assert page.locator('#discoveryMonitor').get_attribute('data-selected-id') is None
                page.wait_for_function('discoveryMonitor.discoveryAtlas.current === null')
                assert fixture.retained() == retained
                return dict(actual_unavailable_response=404, selection_cleared=True,
                    full_snapshot_recovered=True, recovery_does_not_reselect=True)

            if expect_unavailable_stale:
                record = unavailable_and_restore()
                (output / 'reproduction.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                page.unroute_all(behavior='wait')
                browser.close()
                browser = None
                return record

            for mode in ('closed', 'open', 'keyboard-focused'):
                page.locator('#atlasListPanel').evaluate('(node) => node.open = true')
                target = page.locator(f'.monitor-node[data-node-id="{source_id}"]')
                target.click()
                expect(page.locator('#monitorDetail h3').first).to_have_text(fixture.first.name)
                if mode == 'closed':
                    page.locator('#atlasListPanel').evaluate('(node) => node.open = false')
                elif mode == 'keyboard-focused':
                    target.focus()
                details_choice = page.locator('#atlasInspect').get_attribute('aria-expanded')
                if mode == 'keyboard-focused':
                    target.focus()
                old_node = page.evaluate('id => discoveryMonitor.monitorSnapshot.nodes.find(n => n.id === id)', source_id)
                before = current_version(school_id)
                with patch('backend.routes.source_lifecycle.db.session.commit',
                        side_effect=OperationalError('injected selection commit failure', {}, None)):
                    failed = fixture.lifecycle(fixture.first, 'disabled')
                assert failed.status_code == 500 and current_version(school_id) == before
                refresh(keep_focus=mode == 'keyboard-focused')
                expect(page.locator('#monitorDetail h3').first).to_have_text(fixture.first.name)
                saved = fixture.lifecycle(fixture.first, 'disabled')
                assert saved.status_code == 200
                choices = refresh(keep_focus=mode == 'keyboard-focused')
                assert fixture.first.name in choices['choices_html'] and '已停用' in choices['choices_html']
                snapshot = page.evaluate('discoveryMonitor.monitorSnapshot')
                assert source_id not in {row['id'] for row in snapshot['nodes']}
                assert sibling_id in {row['id'] for row in snapshot['nodes']}
                stale = page.locator('#monitorDetail h3').count() > 0
                if expect_stale:
                    assert stale, 'The previous stale-detail defect did not reproduce'
                    record = dict(reproduced=True, mode=mode, api_source_absent=True,
                        choices_disabled=True, details_still_show_old_source=True,
                        structure_version=snapshot['structure_version'])
                    (output / 'reproduction.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                    page.unroute_all(behavior='wait')
                    browser.close()
                    browser = None
                    return record
                assert not stale, 'Removed selection still exposes stale details'
                assert page.locator('#discoveryMonitor').get_attribute('data-selected-id') is None
                expect(page.locator('#atlasBreadcrumb button')).to_have_count(0)
                expect(page.locator('#atlasInspect')).to_have_attribute('aria-expanded', details_choice)
                page.wait_for_function('discoveryMonitor.discoveryAtlas.current === null')
                page.evaluate('node => discoveryMonitor.dispatchEvent(new CustomEvent("monitor-select-node", {detail:{id:node.id,node}}))', old_node)
                expect(page.locator('#monitorDetail h3')).to_have_count(0)
                assert fixture.lifecycle(fixture.first, 'active').status_code == 200
                refresh()
                expect(page.locator('#monitorDetail h3')).to_have_count(0)
                assert fixture.retained() == retained
                results.append(dict(mode=mode, stale_details_cleared=True, stale_selection_event_ignored=True,
                    failed_write_retains_selection=True, restore_does_not_reselect=True,
                    personal_history_preserved=True, details_open_preference_preserved=True))

            # In exploration scope a disabled source remains visible, so keep
            # its selection while updating the detail state at the same version.
            page.locator('#monitorScope').select_option('exploration')
            expect(page.locator('#monitorRefresh')).to_be_enabled()
            page.locator('#atlasListPanel').evaluate('(node) => node.open = true')
            page.locator(f'.monitor-node[data-node-id="{source_id}"]').click()
            page.locator('#atlasListPanel').evaluate('(node) => node.open = false')
            assert fixture.lifecycle(fixture.first, 'disabled').status_code == 200
            refresh()
            expect(page.locator('#monitorDetail h3').first).to_have_text(fixture.first.name)
            expect(page.locator('#monitorDetail .atlas-node-state')).to_have_text('已停用')
            assert page.locator('#discoveryMonitor').get_attribute('data-selected-id') == source_id
            page.locator('#monitorScope').select_option('connected')
            page.wait_for_function('id => !discoveryMonitor.monitorSnapshot.nodes.some(n => n.id === id)', arg=source_id)
            expect(page.locator('#monitorDetail h3')).to_have_count(0)
            recovery = unavailable_and_restore()
            # A lost process cache and a restored epoch both require real full
            # responses. Do not label clearing this cache as a process restart.
            with _transport_lock:
                _transport.clear()
            refresh()
            assert responses[-1]['update_mode'] == 'snapshot'
            reset_epoch(school_id, 'Disposable restoration check')
            db.session.commit()
            refresh()
            assert responses[-1]['update_mode'] == 'snapshot'
            assert fixture.retained() == retained and not errors, errors
            record = dict(passed=True, scenarios=results, exploration_updates_selected_state=True,
                scope_switch_clears_absent_selection=True, delta_received=any(r['update_mode'] == 'delta' for r in responses),
                unavailable_view_recovery=recovery, lost_cache_full_snapshot=True,
                restored_epoch_full_snapshot=True, actual_process_restart_tested=False,
                page_errors=errors, website_requests=0, model_calls=0, authoritative_data_modified=False,
                installer_updated=False, product_sha256={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                    ['frontend/static/js/discovery-monitor.js','frontend/static/js/discovery-atlas.js',
                     'backend/services/structure_snapshot.py','backend/services/source_lifecycle.py']})
            (output / 'browser-acceptance.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            page.unroute_all(behavior='wait')
            browser.close()
            browser = None
            return record
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        connection_guard.stop()
        fixture.doCleanups()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('output')
    parser.add_argument('--expect-stale', action='store_true')
    parser.add_argument('--expect-unavailable-stale', action='store_true')
    args = parser.parse_args()
    faulthandler.dump_traceback_later(45)
    print(json.dumps(check(args.output, args.expect_stale, args.expect_unavailable_stale), ensure_ascii=False))
    faulthandler.cancel_dump_traceback_later()
