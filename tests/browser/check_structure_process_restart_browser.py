"""One real service-process restart with persistent disposable SQLite data.

This complements the existing offline/cache/epoch cases. Fixture navigation
and configured columns are not real university facts or branch samples.
"""
from contextlib import ExitStack
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit
from unittest.mock import patch
import argparse
import hashlib
import json
import multiprocessing
import os
import socket
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
NATIVE_CONNECT=socket.socket.connect


def save(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')


def local_connections_only():
    def connect(sock,address):
        if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1','localhost'):
            return NATIVE_CONNECT(sock,address)
        raise AssertionError('Process-restart fixture attempted an external connection')
    return connect


def serve(config,port,ready,trace):
    from backend import create_app
    from backend.services.structure_snapshot import _transport
    from flask import request
    from werkzeug.serving import make_server,WSGIRequestHandler
    class Quiet(WSGIRequestHandler):
        def log(self,*args,**kwargs):pass
    with ExitStack() as guards:
        guards.enter_context(patch('socket.socket.connect',new=local_connections_only()))
        guards.enter_context(patch('backend.ai.providers.complete',side_effect=AssertionError('No model')))
        guards.enter_context(patch('backend.services.source_inventory.Inventory.__init__',side_effect=AssertionError('No scratch inventory')))
        app=create_app(config)
        assert not _transport,'New process unexpectedly inherited structure transport cache'
        @app.after_request
        def observe(response):
            if request.path.endswith('/structure-snapshot'):
                value=response.get_json(silent=True) or {}
                with Path(trace).open('a',encoding='utf-8') as writer:
                    writer.write(json.dumps(dict(pid=os.getpid(),method=request.method,
                        status=response.status_code,since=request.args.get('since'),mode=value.get('update_mode'),
                        version=value.get('structure_version'),token=value.get('structure_token')))+'\n')
            return response
        server=make_server('127.0.0.1',port,app,threaded=True,request_handler=Quiet)
        ready.put(dict(pid=os.getpid(),port=server.server_port,initial_transport_cache_empty=True))
        try:server.serve_forever()
        finally:server.server_close()


def start(config,port,output,phase):
    context=multiprocessing.get_context('spawn');ready=context.Queue()
    process=context.Process(target=serve,args=(config,port,ready,str(output/f'{phase}-http.jsonl')))
    process.start()
    try:
        state=ready.get(timeout=15)
        assert process.is_alive() and state['pid']==process.pid
        return process,state
    except BaseException:
        if process.is_alive():process.terminate()
        process.join(5)
        raise
    finally:ready.close()


def stop(process):
    if process.is_alive():process.terminate()
    process.join(5)
    assert not process.is_alive(),'Owned service did not stop'
    return dict(pid=process.pid,exit_code=process.exitcode,terminal=True,
                reason='Intentional service termination, not an observation timeout')


def wait_for_page_state(page, expression, *, arg=None, timeout=15000):
    """Poll through the browser protocol without requiring CSP unsafe-eval."""
    deadline=monotonic()+timeout/1000
    while True:
        if page.evaluate(expression,arg):
            return
        if monotonic()>=deadline:
            raise AssertionError('Browser state did not converge within the observation window')
        page.wait_for_timeout(100)


def check(output):
    from tests.test_shared_page_mutations import SharedPageMutationTests
    from backend.database.db import db
    from backend.database.models import (Department,Subscription,Announcement,AnnouncementResource,
                                        UserRead,UserAnnouncementState,BackgroundTask)
    from backend.services.structure_versions import current_version
    from playwright.sync_api import sync_playwright,expect
    output=Path(output).resolve()
    assert output.is_relative_to(ROOT/'.local') and not output.exists()
    output.mkdir(parents=True)
    fixture=SharedPageMutationTests();fixture.setUp()
    first=second=None;phases={};errors=[];external=[];writes=[];failures=[]
    began=monotonic()
    def personal_state():
        return dict(subscriptions=[(r.id,r.user_id,r.school_id,r.department_ids) for r in Subscription.query.order_by(Subscription.id)],
            notices=[(r.id,r.title,r.url,r.content_text,r.content_html) for r in Announcement.query.order_by(Announcement.id)],
            reads=[(r.user_id,r.announcement_id,str(r.read_at)) for r in UserRead.query.order_by(UserRead.id)],
            stars=[(r.user_id,r.announcement_id,r.starred,r.archived,r.trashed_at) for r in UserAnnouncementState.query],
            resources=[(r.announcement_id,r.url,r.sha256,hashlib.sha256(r.payload or b'').hexdigest()) for r in AnnouncementResource.query],
            task_count=BackgroundTask.query.count())
    try:
        app,school=fixture.f.fixture.app,fixture.f.school
        source_id,school_id=fixture.first.id,school.id
        endpoint=fixture.endpoint
        graph=fixture.get()
        assert graph['source_count']==2,'Fixture must contain two normally validated sources'
        owner_edge=next(edge for edge in graph['relations']
            if edge['type']=='source_ownership' and edge['target']==f'source:{source_id}')
        owner=next(node for node in graph['nodes'] if node['id']==owner_edge['source'])
        owner_id=owner['department_id']
        source_records_before=fixture.retained()
        notice=Announcement.query.order_by(Announcement.id).first()
        payload=b'explicit synthetic saved attachment'
        db.session.add(AnnouncementResource(announcement_id=notice.id,url_key='a'*64,
            url='https://example.edu.cn/fixture.txt',filename='fixture.txt',state='saved',
            byte_size=len(payload),sha256=hashlib.sha256(payload).hexdigest(),payload=payload))
        db.session.commit()
        personal_before=personal_state()
        config={key:app.config[key] for key in ['SQLALCHEMY_DATABASE_URI','SOURCE_CATALOG_PATH',
            'DISCOVERY_CACHE_PATH','SOURCE_GOVERNANCE_EVIDENCE_PATH','SECRET_KEY']}
        config.update(TESTING=True,BROWSER_ENABLED=False)
        first,phases['first_start']=start(config,0,output,'first')
        origin=f"http://127.0.0.1:{phases['first_start']['port']}"
        cookie=fixture.client.get_cookie(app.config['SESSION_COOKIE_NAME']);assert cookie
        with patch('socket.socket.connect',new=local_connections_only()),sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True,
                args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            try:
                context=browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='reduce')
                context.add_cookies([dict(name=cookie.key,value=cookie.value,url=origin)])
                def browser_guard(route):
                    request=route.request
                    if urlsplit(request.url).netloc!=urlsplit(origin).netloc:
                        external.append(request.url);return route.abort()
                    if request.method!='GET':
                        writes.append(dict(method=request.method,path=urlsplit(request.url).path));return route.abort()
                    route.continue_()
                context.route('**/*',browser_guard)
                page=context.new_page();page.set_default_timeout(15000)
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('requestfailed',lambda request:failures.append(dict(path=urlsplit(request.url).path,error=request.failure)))
                page.goto(origin+f'/subscriptions/{school_id}/discovery-monitor')
                expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','ready')
                wait_for_page_state(page,'()=>discoveryMonitor.monitorSnapshot?.source_count===2')
                page.evaluate("I18n.setLocale('en')")
                page.locator('#atlasOptions > summary').click();page.locator('#atlasNodeLabels').uncheck()
                page.locator('#atlasOptions > summary').click();page.locator('#atlasFullscreen').click()
                page.evaluate('(id)=>discoveryMonitor.discoveryAtlas.select(id)',f'source:{source_id}')
                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                before=page.evaluate('discoveryMonitor.monitorSnapshot')
                lost=page.evaluate("uiT('连接中断，保留上次数据')")
                phases['first_stop']=stop(first)
                expect(page.locator('#monitorConnection')).to_contain_text(lost,timeout=15000)
                assert page.evaluate('discoveryMonitor.monitorSnapshot.structure_token')==before['structure_token']
                db.session.remove()
                for name in ('离线期间中间更名','重启后的最终学院'):
                    row=db.session.get(Department,owner_id);row.name=name;db.session.commit()
                final_version=current_version(school_id)
                assert final_version['epoch']==before['structure_version']['epoch']
                assert final_version['revision']==before['structure_version']['revision']+2
                assert page.evaluate('discoveryMonitor.monitorSnapshot.structure_token')==before['structure_token']
                second,phases['second_start']=start(config,phases['first_start']['port'],output,'second')
                assert phases['second_start']['pid']!=phases['first_start']['pid']
                wait_for_page_state(page,'''({id,name})=>discoveryMonitor.monitorSnapshot?.nodes.some(
                    n=>n.id===id&&n.label===name)''',arg={'id':owner['id'],'name':'重启后的最终学院'},timeout=35000)
                restored=page.evaluate('discoveryMonitor.monitorSnapshot')
                assert restored['structure_version']==final_version and restored['update_mode']=='snapshot'
                actual_response=context.request.get(origin+endpoint)
                assert actual_response.status==200
                actual=actual_response.json()
                assert restored['nodes']==actual['nodes'] and restored['relations']==actual['relations']
                assert restored['structure_version']==actual['structure_version']
                assert page.evaluate('discoveryMonitor.discoveryAtlas.current.id')==f'source:{source_id}'
                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                assert not page.locator('#atlasNodeLabels').is_checked()
                assert page.locator('.atlas-label:not([hidden])').count()==0
                actual_ids=[n['id'] for n in actual['nodes']]
                assert len(actual_ids)==len(set(actual_ids))
                assert len({e['id'] for e in actual['relations']})==len(actual['relations'])
                assert page.evaluate('[...discoveryMonitor.discoveryAtlas.model.actual.keys()].sort()')==sorted(actual_ids)
                page.screenshot(path=str(output/'after-process-restart.png'),full_page=True)
                context.close()
            finally:browser.close()
        phases['second_stop']=stop(second)
        db.session.remove();assert personal_state()==personal_before
        assert fixture.retained()==source_records_before
        assert restored['source_count']==actual['source_count']==2
        assert not errors and not external and not writes
        trace=[json.loads(line) for line in (output/'second-http.jsonl').read_text(encoding='utf-8').splitlines()]
        assert trace[0]['since']==before['structure_token'] and trace[0]['mode']=='snapshot'
        assert any(row['path']==endpoint for row in failures)
        result=dict(phases=phases,elapsed_seconds=round(monotonic()-began,3),before_version=before['structure_version'],
            after_version=final_version,old_browser_token=before['structure_token'],missed_commits=2,
            real_process_restart=True,same_database_and_origin=True,transport_cache_empty_at_new_process=True,
            actual_connection_failure_observed=True,recovered_without_manual_refresh=True,
            recovered_via_full_snapshot=True,browser_nodes_relations_match_fresh_http=True,
            selection_preserved=True,labels_remain_off=True,details_remain_closed=True,
            duplicate_nodes=0,duplicate_relations=0,subscription_read_star_body_attachment_unchanged=True,
            normally_validated_sources=2,source_configuration_receipts_unchanged=True,
            changed_entity_type='organization',page_security_policy_unchanged=True,
            browser_errors=errors,browser_writes=writes,external_requests=external,request_failures=failures,
            website_requests=0,model_calls=0,fixture_only=True,real_university_acceptance=False)
        save(output/'results.json',result)
        save(output/'artifact-hashes.json',{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()})
        print(json.dumps({key:result[key] for key in ['real_process_restart','recovered_without_manual_refresh','missed_commits','selection_preserved','subscription_read_star_body_attachment_unchanged','elapsed_seconds']}))
    except BaseException as error:
        save(output/'failure.json',dict(error=repr(error),phases=phases,errors=errors,external_requests=external,writes=writes,request_failures=failures))
        raise
    finally:
        for process in (first,second):
            if process is not None and process.is_alive():stop(process)
        fixture.tearDown();fixture.doCleanups()


if __name__=='__main__':
    multiprocessing.freeze_support()
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    check(parser.parse_args().output)
