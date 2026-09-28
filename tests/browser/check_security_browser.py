"""Browser regression: external/imported text cannot execute in the application origin."""
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('WATCHER_DATA_DIR', str(ROOT / '.local/security-browser/data'))
os.environ.setdefault('WATCHER_ENV_FILE', str(ROOT / '.local/security-browser/data/.env'))
from flask import render_template_string
from playwright.sync_api import sync_playwright
import test_storage_management as fixtures
from backend.database.models import Announcement
from test_security_regressions import ATTACKS


fixture = fixtures.StorageManagementTests()
fixture.setUp()
results = []
try:
    csp = fixture.client.get('/admin/storage').headers['Content-Security-Policy']
    script = (ROOT / 'frontend/static/js/content.js').read_text(encoding='utf-8')
    with sync_playwright() as p:
        channel = os.environ.get('WATCHER_TEST_BROWSER_CHANNEL')
        browser = p.chromium.launch(headless=True, **({'channel':channel} if channel else {}))
        for mode in ('legacy_cache', 'imported', 'fresh'):
            for index, payload in enumerate(ATTACKS):
                data = fixture.sample()
                data['announcements'][0]['url'] = f'https://example.edu.cn/audit/{mode}/{index}'
                data['announcements'][0]['content_html'] = payload
                response = fixture.upload(fixtures.packed(data))
                assert response.status_code == 200
                ann = Announcement.query.filter_by(url=data['announcements'][0]['url']).one()
                safe_response = fixture.client.get(f'/api/announcements/{ann.id}/content').json
                if mode == 'legacy_cache':
                    ann.content_html = payload  # Emulate a pre-fix cached entry.
                elif mode == 'fresh':
                    ann.content_html = ''
                    ann.content_text = ''
                    ann.content_cached_at = None
                html = render_template_string("{% from '_content_loader.html' import content_loader %}{{ content_loader(announcement) }}", announcement=ann)
                page = browser.new_page()
                def serve(route):
                    if route.request.url.endswith('/content.js'):
                        route.fulfill(status=200, content_type='text/javascript', body=script)
                    elif '/api/' in route.request.url:
                        route.fulfill(status=200, content_type='application/json', body=json.dumps(safe_response))
                    elif route.request.resource_type == 'image':
                        route.fulfill(status=404, body='')
                    else:
                        route.fulfill(status=200, headers={'Content-Type':'text/html; charset=utf-8','Content-Security-Policy':csp},
                            body='<!doctype html><meta charset="utf-8">'+html+'<script src="/content.js"></script>')
                page.route('**/*', serve)
                page.goto('http://audit.invalid/')
                page.wait_for_timeout(100)
                for link in page.locator('.body-loader-content a').all():
                    link.click(timeout=2000)
                assert not page.evaluate('Boolean(window.__audit_marker)'), (mode,index)
                assert page.locator('.body-loader-content script, .body-loader-content svg, .body-loader-content math').count() == 0
                results.append({'mode':mode,'payload':index,'blocked':True})
                page.close()
                fixture.app.extensions['sqlalchemy'].session.rollback()
        template = (ROOT / 'frontend/templates/_admin_schools.html').read_text(encoding='utf-8')
        functions = '\n'.join(re.search(r'^function '+name+r'\([^\n]*\) \{.*?^\}', template, re.M | re.S).group()
                              for name in ('escHtml','addDiscoveryLog','updateDiscoveryPhase','showDiscoveryError'))
        page = browser.new_page()
        document = ''.join(f'<div id="{name}"></div>' for name in (
            'discoveryLog','discoveryStatus','discoveryProgressFill','discoveryPhase',
            'discoveryResults','discoveryError','discoveryTitle'))
        page.route('**/*',lambda route:route.fulfill(status=200,headers={'Content-Type':'text/html','Content-Security-Policy':csp},
                   body=document+'<script>var eventSource=null;'+functions+'</script>'))
        page.goto('http://audit.invalid/')
        message = '<img src=x onerror=window.__audit_marker=1>教务处'
        page.evaluate('(s)=>{addDiscoveryLog(s);updateDiscoveryPhase({message:s});showDiscoveryError(s)}',message)
        page.wait_for_timeout(100)
        assert not page.evaluate('Boolean(window.__audit_marker)')
        assert page.locator('img').count() == 0
        assert message in page.locator('#discoveryLog').inner_text()
        results.append({'mode':'discovery_messages','blocked':True})
        # Imported names must stay data even when a management button is clicked.
        from backend.database.db import db
        from bs4 import BeautifulSoup
        attack_name = "School');window.__audit_marker=1;// &apos;"
        fixture.school.name = attack_name
        db.session.commit()
        markup = fixture.client.get('/admin').text
        button = BeautifulSoup(markup, 'html.parser').select_one('[data-school-name]')
        assert button is not None
        page.set_content(str(button)+'<script>function editSchool(id,name){window.__received_name=name;}</script>')
        page.locator('button').click()
        assert page.evaluate('window.__received_name') == attack_name
        assert not page.evaluate('Boolean(window.__audit_marker)')
        results.append({'mode':'school_edit_name','blocked':True})
        load_depts = re.search(r'^async function loadDepts\([^\n]*\) \{.*?^\}', template, re.M | re.S).group()
        escape_html = re.search(r'^function escHtml\([^\n]*\) \{.*?^\}', template, re.M | re.S).group()
        page.set_content('<div id="deptList"></div><script>'+escape_html+load_depts+
            ';function editDept(id,name){window.__received_name=name;}function deleteDept(){}function showToast(){throw Error("load failed")}</script>')
        awaitable = '(s)=>{window.fetch=async()=>({json:async()=>[{id:1,name:s}]});return loadDepts(1)}'
        page.evaluate(awaitable, attack_name)
        page.locator('[data-dept-action="edit"]').click()
        assert page.evaluate('window.__received_name') == attack_name
        assert not page.evaluate('Boolean(window.__audit_marker)')
        results.append({'mode':'department_edit_name','blocked':True})
        browser.close()
finally:
    fixture.tearDown()
print(json.dumps({'cases':len(results),'results':results},ensure_ascii=False))
