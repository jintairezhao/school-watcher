"""Review errors and automatically grouped columns remain usable on small screens."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from test_source_governance import SourceGovernanceTests
from test_wordpress_publications import ARTICLE, TITLE, article_html
from backend.database.db import db
from backend.database.source_governance_models import SourceProposal
from backend.services import source_governance as governance

fixture = SourceGovernanceTests()
fixture.setUp()
output = ROOT / '.local/source-review-fix/screenshots'
output.mkdir(parents=True, exist_ok=True)
try:
    proposal = governance.propose_source(fixture.school.id, {'name': TITLE, 'list_url': ARTICLE}, origin='submitted_entry')
    with patch.object(governance, '_fetch', return_value=article_html()):
        result = governance.process_source_review({'proposal_id': proposal.id})
    target = db.session.get(SourceProposal, result['validation']['related_proposal_ids'][0])
    target.state = 'needs_review'
    target.validation_json = json.dumps({'errors': ['source_login_required']})
    from backend.database.models import BackgroundTask
    BackgroundTask.query.filter_by(identity=f'source_review:{target.id}').update({'state': 'done'})
    db.session.commit()
    client = fixture.app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = fixture.admin.id
        session['csrf_token'] = 'source-review-test'
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        for width in (1280, 390):
            page = browser.new_page(viewport={'width': width, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def serve(route):
                url = urlsplit(route.request.url)
                response = client.open(url.path + ('?' + url.query if url.query else ''),
                    method=route.request.method, data=route.request.post_data,
                    headers={key: value for key, value in route.request.headers.items()
                             if key.lower() in ('content-type', 'x-csrf-token')})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
            page.route('**/*', serve)
            page.add_init_script("if (window === window.top) localStorage.setItem('theme', 'dark')")
            page.goto('http://localhost/admin/sources')
            page.locator('#proposalState').select_option('login_required')
            expect(page.locator('#proposalList button')).to_have_count(1)
            page.locator('#proposalList button').click()
            page.locator('#proposalWorkflow').filter(has_text='官网要求登录').wait_for()
            assert page.locator('[data-review="recheck"]').inner_text() == '自动检查'
            assert not page.locator('#reviewNote').is_visible()
            assert page.locator('#sourcePicker, #manualSourceReview').count() == 0
            page.screenshot(path=str(output / f'review-{width}.png'), full_page=True)
            page.locator('#proposalState').select_option('superseded')
            page.locator('#proposalList button').click()
            page.get_by_role('button', name='查看所属栏目：考试').wait_for()
            assert not page.locator('#reviewActions').is_visible()
            page.get_by_role('button', name='查看所属栏目：考试').click()
            page.locator('#proposalWorkflow').filter(has_text='官网要求登录').wait_for()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            page.close()
        browser.close()
    print(json.dumps({'status': 'passed', 'widths': [1280, 390], 'paid_api_calls': 0}))
finally:
    fixture.tearDown()
