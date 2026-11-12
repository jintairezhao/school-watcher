"""Real browser retry over the real review route; no worker/network/model runs."""
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
import faulthandler,hashlib,json,os,sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright,expect
from sqlalchemy.exc import OperationalError
from test_source_review_transaction import SourceReviewTransactionTests
from backend.database.db import db
from backend.database.models import BackgroundTask
from backend.database.source_governance_models import SourceConfigVersion
from backend.services.localization import stage_preferences
from backend.services.source_governance import source_config
from backend.services.structure_versions import current_version


def check(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    assert not (output/'browser-acceptance.json').exists(),'Reuse completed acceptance'
    baseline=json.loads((ROOT/'.local/structure-facts-20261021/review-request-transaction-20261101/stage-delivery.json').read_text(encoding='utf-8'))
    sha=lambda p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
    assert all(sha(p)==h for p,h in baseline['protected_fingerprints'].items())
    assert all(sha(p)==h for p,h in baseline['product_fingerprints'].items())
    rows=[]
    print('Starting the browser runtime',flush=True)
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'),headless=True)
        print('Browser launched',flush=True)
        try:
            for locale in ('zh-CN','en'):
                for action in ('recheck','select_option'):
                    fixture=SourceReviewTransactionTests();fixture.setUp();page=None
                    try:
                        validation=json.loads(fixture.proposal.validation_json)
                        validation['workflow']['question']['options'].append(dict(id='undergraduate',field='list_url',
                            value=fixture.source.list_url,label='本科生通知'))
                        fixture.proposal.validation_json=json.dumps(validation,ensure_ascii=False)
                        stage_preferences(fixture.admin.id,{'locale':locale});db.session.commit()
                        original=fixture.state()
                        page=browser.new_page(viewport={'width':1280,'height':900},reduced_motion='reduce')
                        errors=[];writes=[];responses=[];fail_queue=True
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        def serve(route):
                            nonlocal fail_queue
                            req=route.request;parsed=urlsplit(req.url)
                            assert parsed.hostname=='localhost', 'No external request: '+req.url
                            path=parsed.path+('?' + parsed.query if parsed.query else '')
                            headers={k:v for k,v in req.headers.items() if k.lower() in ('content-type','x-csrf-token')}
                            if req.method!='GET':writes.append(dict(path=parsed.path,body=req.post_data))
                            if parsed.path==fixture.url and req.method=='POST' and fail_queue:
                                with patch('backend.services.tasks.enqueue',side_effect=OperationalError('injected browser queue failure',{},None)):
                                    response=fixture.client.open(path,method=req.method,data=req.post_data,headers=headers)
                            else:
                                response=fixture.client.open(path,method=req.method,data=req.post_data,headers=headers)
                            if parsed.path==fixture.url and req.method=='POST':
                                responses.append(dict(status=response.status_code,data=response.json))
                            route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                        page.route('**/*',serve)
                        page.goto('http://localhost/admin/sources')
                        expect(page.locator('#proposalList button')).to_have_count(1)
                        page.locator('#proposalList button').click()
                        expect(page.locator('#proposalDetail')).to_be_visible()
                        if action=='recheck':button=page.locator('[data-review="recheck"]')
                        else:button=page.locator('#proposalQuestion').get_by_role('button',name='研究生通知',exact=True)
                        button.click()
                        message=('操作未完成，请稍后重试。' if locale=='zh-CN' else 'Could not complete this action. Try again shortly.')
                        expect(page.locator('#reviewStatus')).to_have_text(message)
                        expect(page.locator('#proposalDetail')).to_be_visible()
                        expect(button).to_be_enabled()
                        assert fixture.state()==original,'Failed request changed persisted state'
                        assert responses[-1]['status']==500
                        fail_queue=False;button.click()
                        expect(page.locator('#proposalList')).to_contain_text(
                            '正在自动检查或补充材料' if locale=='zh-CN' else 'Checking automatically or gathering more evidence')
                        if action=='recheck':expect(page.locator('#proposalDetail')).to_be_hidden()
                        else:
                            expect(page.locator('#proposalWorkflow')).to_contain_text(
                                '正在自动检查或补充材料' if locale=='zh-CN' else 'Checking automatically or gathering more evidence')
                            expect(page.locator('#proposalQuestion button')).to_have_count(0)
                        task=BackgroundTask.query.one()
                        assert task.state=='pending' and task.identity==f'source_review:{fixture.ident}'
                        assert SourceConfigVersion.query.count()==0
                        assert source_config(fixture.source)==original['config']
                        assert current_version(fixture.school.id)==dict(original['structure_version'],
                            revision=original['structure_version']['revision']+1)
                        assert responses[-1]['status']==202 and responses[-1]['data']['workflow']['status']=='checking'
                        assert [r['status'] for r in responses]==[500,202]
                        assert len(writes)==2 and all(w['path']==fixture.url for w in writes)
                        assert not errors,errors
                        rows.append(dict(locale=locale,action=action,failed_message=message,http_statuses=[500,202],
                            failure_unchanged=True,detail_retained_on_failure=True,retry_available=True,
                            one_pending_task=True,source_activation_claimed=False,active_rules_unchanged=True,
                            source_version_count=0,page_errors=errors))
                        print(f'Passed: {locale} {action}',flush=True)
                    except Exception:
                        if page:page.screenshot(path=str(output/f'failure-{locale}-{action}.png'),full_page=True)
                        raise
                    finally:
                        if page:page.close()
                        fixture.tearDown()
        finally:browser.close()
    assert all(sha(p)==h for p,h in baseline['protected_fingerprints'].items())
    result=dict(completed=True,scenarios=rows,browser='headless Microsoft Edge',actual_product_route=True,
        browser_requests_intercepted_to_flask_test_client=True,live_http_server_or_installed_program_tested=False,
        protected_fingerprints=baseline['protected_fingerprints'],authoritative_case_unchanged=True,
        product_fingerprints=baseline['product_fingerprints'],test_fingerprint=sha('tests/browser/check_source_review_transaction_browser.py'),
        website_requests=0,model_calls=0,worker_tasks_executed=0,installed_program_updated=False,public_release=False)
    (output/'browser-acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(completed=True,scenarios=len(rows),locales=['zh-CN','en'],external_requests=0,model_calls=0),ensure_ascii=False))


if __name__=='__main__':
    faulthandler.dump_traceback_later(45)
    try:check(sys.argv[1] if len(sys.argv)>1 else ROOT/'.local/structure-facts-20261021/review-request-transaction-20261101/browser')
    finally:faulthandler.cancel_dump_traceback_later()
