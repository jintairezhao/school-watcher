"""Actual monitor routes and saved state; no university or provider traffic."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_discovery_monitor import DiscoveryMonitorTests
from backend.database.db import db
from backend.database.models import BackgroundTask, Department
from playwright.sync_api import expect, sync_playwright


def check():
    output = ROOT / '.local/discovery-monitor'
    output.mkdir(parents=True, exist_ok=True)
    report = []
    with tempfile.TemporaryDirectory(prefix='monitor-browser-') as scratch, \
         patch.dict(os.environ, {'WATCHER_DATA_DIR':scratch, 'WATCHER_ENV_FILE':str(Path(scratch) / '.env'),
                                 'FIELD_ENC_KEY':'isolated-monitor-field-key'}), \
         patch('backend.ai.providers.complete', side_effect=AssertionError('No paid request')), sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        fixture = DiscoveryMonitorTests(); fixture.setUp()
        try:
            base = fixture.school.url
            pages = [{'url':base + '/', 'label':'学校官网', 'kind':'root', 'path_json':[], 'state':'fetched'}]
            for group, unit, column, state in [
                ('院系设置','财政金融学院','通知公告','fetched'),
                ('院系设置','外国语学院','教学通知','fetched'),
                ('院系设置','哲学院','研究生培养','pending'),
                ('组织机构','学生工作处','奖学金与资助','fetched'),
                ('组织机构','研究生院','招生与推免','fetched'),
                ('组织机构','教务处','选课通知','failed'),
                ('组织机构','就业指导中心','实习就业','pending')]:
                url = base + '/unit/' + str(len(pages))
                pages.extend([{'url':url,'label':unit,'kind':'unit','path_json':['学校官网',group], 'state':'fetched'},
                    {'url':url + '/notice','label':column,'kind':'channel','path_json':['学校官网',group,unit],
                     'state':state,'status_code':502 if state == 'failed' else 200,'checked_at':'2026-10-17T05:00:00Z'}])
                if state == 'fetched':
                    db.session.add(Department(school_id=fixture.school.id, name=column, group_name=unit,
                        list_url=url + '/notice', list_selector='li'))
            fixture.publish(pages)
            row = fixture.task(state='running', lease_until=datetime.utcnow() + timedelta(hours=1),
                checkpoint={'discovery_progress':{'phase':'crawl','checked_pages':28,'pending_pages':12,
                    'updated_at':datetime.utcnow().isoformat() + 'Z','current_label':'哲学院',
                    'current_pages':[{'url':base + '/unit/5','label':'哲学院','path_json':['学校官网','院系设置']}]}})
            queued = BackgroundTask(identity='onboard:monitor-queued',kind='onboard',state='pending',
                payload={'school_id':fixture.school.id,'page':{'url':base + '/unit/5/notice',
                    'label':'研究生培养','path_json':['学校官网','院系设置','哲学院']}})
            db.session.add(queued)
            db.session.commit()
            before = BackgroundTask.query.count()
            for width, theme in ((1440,'dark'), (390,'light')):
                page = browser.new_page(viewport={'width':width,'height':1100}, reduced_motion='reduce')
                page.clock.install()
                errors, writes, gets = [], [], []
                broken = {'value':False}
                page.on('pageerror', lambda exc: errors.append(str(exc)))
                def serve(route):
                    req = route.request; parsed = urlsplit(req.url)
                    assert parsed.hostname == 'localhost', 'No external requests'
                    if parsed.path == '/_desktop/components':
                        return route.fulfill(json={'phase':'ready','message':'可用'})
                    if parsed.path.endswith('/discovery-monitor') and parsed.path.startswith('/api/'):
                        gets.append(parsed.path)
                        if broken['value']:
                            return route.fulfill(status=503, json={'error':'isolated outage'})
                    if req.method != 'GET':
                        writes.append(parsed.path)
                    response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                        method=req.method, data=req.post_data, headers={key:val for key,val in req.headers.items()
                            if key.lower() in ('content-type','x-csrf-token')})
                    route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                page.route('**/*', serve)
                page.add_init_script('localStorage.setItem("theme", ' + json.dumps(theme) + ')')
                page.goto('http://localhost' + fixture.page)
                expect(page.locator('#monitorChecked')).to_have_text('28')
                expect(page.locator('#monitorConnected')).to_have_text('4')
                expect(page.locator('#monitorCandidates')).to_have_text('1')
                expect(page.locator('#monitorConnection')).to_contain_text('每 3 秒')
                page.locator('#monitorAuto').uncheck()
                assert not writes and BackgroundTask.query.count() == before
                # Labels describe the isolated fixture rather than claiming real school results.
                page.locator('.school-heading p').evaluate("node => node.textContent = '隔离演示数据 · 展示实际界面布局，不代表该校实测结果'")
                page.locator('#monitorSearch').fill('资助')
                expect(page.locator('.monitor-node')).to_have_count(1)
                page.locator('.monitor-node').click()
                expect(page.locator('#monitorDetail')).to_contain_text('学生工作处')
                expect(page.locator('#monitorDetail')).to_contain_text('已接入栏目')
                page.locator('#monitorSearch').fill('')
                page.locator('#monitorFilter').select_option('failed')
                expect(page.locator('.monitor-node')).to_have_count(1)
                expect(page.locator('.monitor-node')).to_contain_text('选课通知')
                page.locator('#monitorFilter').select_option('all')
                page.locator('#monitorFilter').select_option('pending')
                expect(page.locator('.monitor-node').filter(has_text='研究生培养')).to_contain_text('等待探索')
                page.locator('#monitorFilter').select_option('all')
                # Automatic refresh can be resumed and manual refresh sees new saved work.
                row.checkpoint = {'discovery_progress':dict(row.checkpoint['discovery_progress'], checked_pages=29,
                    pending_pages=11, updated_at=datetime.utcnow().isoformat() + 'Z')}
                db.session.commit()
                page.locator('#monitorRefresh').click()
                expect(page.locator('#monitorChecked')).to_have_text('29')
                broken['value'] = True
                page.locator('#monitorRefresh').click()
                expect(page.locator('#monitorConnection')).to_contain_text('保留上次数据')
                expect(page.locator('#monitorChecked')).to_have_text('29')
                broken['value'] = False
                page.locator('#monitorRefresh').click()
                expect(page.locator('#monitorError')).to_be_hidden()
                page.locator('#monitorAuto').check()
                expect(page.locator('#monitorConnection')).to_contain_text('每 3 秒')
                row.checkpoint = {'discovery_progress':dict(row.checkpoint['discovery_progress'], checked_pages=30)}
                db.session.commit()
                page.clock.fast_forward(3100)
                expect(page.locator('#monitorChecked')).to_have_text('30')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'No viewport overflow'
                page.evaluate('scrollTo(0,0)')
                page.screenshot(path=str(output / f'monitor-{theme}-{width}.png'), full_page=True)
                expect(page.locator('#monitorControl')).to_have_text('暂停探索')
                page.locator('#monitorControl').click()
                expect(page.locator('#monitorControl')).to_have_text('正在暂停…')
                assert writes == [fixture.endpoint + '/control']
                assert not errors, errors
                report.append({'width':width,'theme':theme,'page_errors':errors,'real_routes':True,
                    'passive_reads_do_not_dispatch':True,'filter_and_node_detail':True,'new_checkpoint_visible':True,'automatic_refresh_observed':True,
                    'outage_keeps_last_data':True,'pause_is_explicit_post':True,'pending_is_distinct_from_waiting':True})
                page.close()
                row.state='running'; row.lease_until=datetime.utcnow() + timedelta(hours=1)
                row.checkpoint={'discovery_progress':{'checked_pages':28,'pending_pages':12,'current_label':'哲学院',
                    'current_pages':[{'url':base + '/unit/5','label':'哲学院','path_json':['学校官网','院系设置']}],
                    'updated_at':datetime.utcnow().isoformat()+'Z'}}
                row.payload={'school_id':fixture.school.id}
                queued.state='pending'; queued.phase='fetch'; queued.payload={'school_id':fixture.school.id,
                    'page':{'url':base + '/unit/5/notice','label':'研究生培养','path_json':['学校官网','院系设置','哲学院']}}
                db.session.commit()
            assert fixture.client.get('/static/img/discovery-system.svg').status_code == 404
            assert not (ROOT / 'frontend/static/img/discovery-system.svg').exists()
        finally:
            fixture.tearDown(); browser.close()
    (output / 'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    check()
