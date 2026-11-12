"""Bound the existing native worker, without implementing an exploration policy."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import gzip
import hashlib
import json
import multiprocessing
import os
import sqlite3
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'.local/structure-facts-20261021'
NATIVE_SAMPLE_KINDS = ('discover','directory','navigation_review','onboard','source_review','collect')
sys.path.insert(0,str(ROOT))
read = lambda p:json.loads(Path(p).read_text(encoding='utf-8-sig'))
sha = lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(path,value):
    temporary = Path(str(path)+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    temporary.replace(path)
def now():
    return datetime.now(timezone.utc).isoformat()


class Bounds:
    """Resource admission only. Missing observations remain unknown."""
    def __init__(self,scope,instance,output,deadline):
        self.scope,self.instance,self.output,self.deadline = scope,Path(instance),Path(output),deadline
        self.control=self.instance/'control'
        self.control.mkdir(parents=True,exist_ok=True)
        self.output.mkdir(parents=True,exist_ok=True)
        self.web_file=self.control/'pilot-web-ledger.json'
        if not self.web_file.exists():save(self.web_file,dict(documents=[],http_attempts=[],boundary=None))
        self.document=ContextVar('sampling_document',default=None)
        self.boundary=None

    def stop(self,reason,**detail):
        from backend.services import tasks
        handle=tasks.current_execution()
        park=reason=='outside_declared_host' and self.scope.get('park_out_of_scope_tasks') and handle
        if not park:self.boundary=reason
        ledger=read(self.web_file);ledger['boundary']=dict(reason=reason,at=now(),semantic_value='unknown',**detail)
        if park:
            ledger.setdefault('scope_exclusions',[]).append(dict(ledger['boundary'],
                task_id=handle['id'],generation=handle['generation'],document=self.document.get()))
        save(self.web_file,ledger)
        if park:
            tasks.defer(state='waiting',phase='sampling_scope',reason='采样范围外入口保留未知，继续处理范围内已排入的任务',
                error_code='sampling_outside_declared_host',keep_place=True)
        if handle:
            tasks.defer(phase='sampling_boundary',delay=86400,reason='采样资源边界，进度已保存；后续内容保持未知',
                error_code='sampling_'+reason,keep_place=True)
        raise SamplingBoundary(reason)

    def check_time(self):
        if time.monotonic()>=self.deadline:self.stop('wall_budget')
        if datetime.now(timezone.utc)>=datetime.fromisoformat(self.scope['expires_at']):self.stop('scope_expired')

    def allowed_host(self,host):
        if host in self.scope['website']['allowed_hosts']:return True
        family=self.scope['website'].get('allowed_school_domain')
        return bool(family and host and (host==family or host.endswith('.'+family)))

    def new_document(self,request):
        from backend.ai.material_capture import current_action_reference
        self.check_time()
        if not self.allowed_host(urlsplit(request.url).hostname):self.stop('outside_declared_host',url=request.url)
        ledger=read(self.web_file)
        if len(ledger['documents'])>=self.scope['website']['maximum_documents']:self.stop('document_budget')
        from backend.services import tasks
        handle=tasks.current_execution() or {}
        row=dict(sequence=len(ledger['documents'])+1,started_at=now(),request=request.to_dict(),
            task_id=handle.get('id'),generation=handle.get('generation'),result=None,
            action_reference=current_action_reference())
        ledger['documents'].append(row);save(self.web_file,ledger)
        return row['sequence']

    def http_attempt(self,url,method,headers):
        self.check_time()
        host=headers.get('Host') or headers.get('host') or urlsplit(str(url)).hostname
        ledger=read(self.web_file)
        if not self.allowed_host(host):
            from scripts.checks.native_sampling_redirects import allowed_observed_redirect
            if not allowed_observed_redirect(self.scope,ledger,self.document.get(),str(url),host,method):
                self.stop('outside_declared_host',wire_url=str(url),host=host)
        if len(ledger['http_attempts'])>=self.scope['website']['maximum_http_attempts']:self.stop('http_budget')
        ident=len(ledger['http_attempts'])+1
        ledger['http_attempts'].append(dict(sequence=ident,document=self.document.get(),at=now(),
            wire_url=str(url),host=host,method=method,status=None))
        save(self.web_file,ledger)
        return ident

    def finish_attempt(self,ident,**values):
        ledger=read(self.web_file);ledger['http_attempts'][ident-1].update(values);save(self.web_file,ledger)

    def finish_document(self,ident,result):
        meta=result.to_dict();html=meta.pop('html','')
        if html:
            digest=hashlib.sha256(html.encode()).hexdigest();name=digest+'.html.gz'
            target=self.output/name
            if not target.exists():target.write_bytes(gzip.compress(html.encode(),mtime=0))
            meta.update(decoded_html_sha256=digest,decoded_html_file=name)
        attachment=dict(meta.get('attachment') or {})
        payload=attachment.pop('payload',None)
        if isinstance(payload,bytes):
            digest=hashlib.sha256(payload).hexdigest();name=digest+'.bin.gz'
            (self.output/name).write_bytes(gzip.compress(payload,mtime=0))
            attachment.update(payload_sha256=digest,payload_file=name)
        meta['attachment']=attachment
        meta['headers']={k:v for k,v in meta.get('headers',{}).items() if k.lower() in ('content-type','location','retry-after','etag','last-modified')}
        ledger=read(self.web_file);ledger['documents'][ident-1].update(finished_at=now(),result=meta,
            observation_stage='http_transport_before_content_classification',
            raw_wire_html_bytes_retained=False)
        save(self.web_file,ledger)

    def check_model(self):
        from backend.ai.configuration import AIConfigError
        if time.monotonic()>=self.deadline or datetime.now(timezone.utc)>=datetime.fromisoformat(self.scope['expires_at']):
            self.boundary='model_time_boundary'
            raise AIConfigError('采样时间边界，进度保留','budget_exhausted')
        ledger=read(self.control/'model-attempts.json')
        if ledger['used']>=self.scope['model']['maximum_dispatch_attempts']:
            self.boundary='model_budget'
            raise AIConfigError('采样模型额度已用完，进度保留','budget_exhausted')

    def reserve_model(self,profile):
        from filelock import FileLock
        from backend.ai.providers import ProviderError
        if time.monotonic()>=self.deadline or datetime.now(timezone.utc)>=datetime.fromisoformat(self.scope['expires_at']):
            self.boundary='model_time_boundary'
            raise ProviderError('sampling_time_boundary',diagnostics={'request_phase':'sampling_guard_before_transport'})
        if (profile['provider'],profile['model'],profile['region'])!=('deepseek','deepseek-flash','default'):
            raise ProviderError('sampling_profile_outside_declared_scope')
        path=self.control/'model-attempts.json'
        with FileLock(str(path)+'.lock',timeout=5):
            ledger=read(path)
            if ledger['used']>=self.scope['model']['maximum_dispatch_attempts']:
                self.boundary='model_budget'
                raise ProviderError('sampling_model_request_limit',diagnostics={'request_phase':'sampling_guard_before_transport'})
            ledger['used']+=1
            ledger['attempts'].append(dict(sequence=ledger['used'],at=now(),provider=profile['provider'],
                model=profile['model'],profile_id=profile.get('id')))
            save(path,ledger)

    @contextmanager
    def install(self):
        from unittest.mock import patch
        import aiohttp
        import requests
        from backend.ai import providers,runtime
        from backend.scraper.acquisition import http_transport
        from backend.services import desktop_maintenance
        from scripts.checks.native_sampling_page_scope import page_scope_exclusions
        original_fetch,original_request=http_transport.fetch_http,aiohttp.ClientSession._request
        original_complete,original_check=providers.complete,runtime._check_connection
        original_send,original_before=requests.Session.send,desktop_maintenance.before_fetch
        def fetch(request,**kwargs):
            ident=self.new_document(request)
            marker=self.document.set(ident)
            try:
                if kwargs.get('capture_binary'):
                    kwargs['max_bytes']=min(kwargs.get('max_bytes') or self.scope['website']['maximum_response_bytes'],
                        self.scope['website']['maximum_response_bytes'])
                effective=replace(request,timeout_seconds=min(request.timeout_seconds,max(.001,self.deadline-time.monotonic())))
                result=original_fetch(effective,**kwargs)
                self.finish_document(ident,result)
                return result
            finally:self.document.reset(marker)
        async def request(session,method,url,**kwargs):
            ident=self.http_attempt(url,method,kwargs.get('headers') or {})
            try:
                result=await original_request(session,method,url,**kwargs)
                self.finish_attempt(ident,status=result.status,finished_at=now(),
                    location=result.headers.get('Location'),content_type=result.headers.get('Content-Type'))
                return result
            except BaseException as error:
                self.finish_attempt(ident,error_type=type(error).__name__,finished_at=now())
                raise
        def check(binding,skill):
            original_check(binding,skill)
            self.check_model()
        def complete(profile,key,messages,**kwargs):
            self.reserve_model(profile)
            return original_complete(profile,key,messages,**kwargs)
        def send(session,request,**kwargs):
            # Unexpected legacy/auxiliary HTTP must never bypass the website ledger.
            if request.url!='https://api.deepseek.com/chat/completions':self.stop('unrecorded_transport_path')
            return original_send(session,request,**kwargs)
        def before(pause):
            original_before(pause);self.check_time()
        with patch.object(http_transport,'fetch_http',fetch),patch.object(aiohttp.ClientSession,'_request',request),\
                patch.object(runtime,'_check_connection',check),patch.object(providers,'complete',complete),\
                patch.object(requests.Session,'send',send),patch.object(desktop_maintenance,'before_fetch',before),\
                page_scope_exclusions(self):
            yield self


class SamplingBoundary(BaseException):
    pass


def metadata(scope):
    from contextlib import closing
    instance=(ROOT/scope['instance_path']).resolve()
    assert instance.is_relative_to(BASE.resolve())
    with closing(sqlite3.connect((instance/'school_watcher.db').as_uri()+'?mode=ro',uri=True)) as connection:
        connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
        profiles=[dict(r) for r in connection.execute('select id,provider,model,region,version,tested_version,enabled from ai_profiles where deleted_at is null')]
        bindings=[dict(r) for r in connection.execute('select purpose,profile_id from ai_bindings')]
        ready=any(b['purpose']=='directory' and any(p['id']==b['profile_id'] and p['enabled'] and p['version']==p['tested_version']
            and (p['provider'],p['model'],p['region'])==('deepseek','deepseek-flash','default') for p in profiles) for b in bindings)
        from scripts.checks.native_sampling_admission import read_budget_admission
        used=read(instance/'control/model-attempts.json')['used']
        budget=read_budget_admission(connection,scope,ROOT,used)
    return dict(observed_at=now(),profiles=profiles,bindings=bindings,directory_binding_ready=ready,
        model_attempts=used,model_budget_admission=budget,credentials_read=False)


def require_model_admission(scope):
    state=metadata(scope)
    report=state['model_budget_admission']
    if report.get('bootstrap_ready'):
        # A new school has no prior round/envelope. This only admits setup;
        # production _execute still reserves each actual request before send.
        assert scope.get('new_school_in_existing_instance') is True and scope.get('continuation_school_id') is None
        assert state['directory_binding_ready'], 'Native model binding is not tested and ready'
        check_added_school(scope)
        return report
    assert report['ready'], 'Native model budget admission: '+report['status']
    return report


def continuation_state(scope):
    """Public state fingerprints only; no profile credential columns or files."""
    from contextlib import closing
    if scope.get('model_free_reader_window'):
        assert scope.get('model_free_reader_window') is True and scope.get('model_free_native_slice') is True
        validate_model_free_scope(scope)
    instance=(ROOT/scope['instance_path']).resolve()
    assert instance.is_relative_to(BASE.resolve())
    with closing(sqlite3.connect((instance/'school_watcher.db').as_uri()+'?mode=ro',uri=True)) as connection:
        connection.row_factory=sqlite3.Row
        connection.execute('PRAGMA query_only=ON');connection.execute('BEGIN')
        schools=[dict(r) for r in connection.execute('SELECT id,name,url FROM schools ORDER BY id')]
        selected=scope.get('continuation_school_id')
        if selected is not None:
            assert type(selected) is int and selected>0 and any(s['id']==selected for s in schools), 'Invalid continuation school'
        recovery=scope.get('resume_expired_task_ids',[])
        assert isinstance(recovery,list) and all(type(i) is int and i>0 for i in recovery) and len(set(recovery))==len(recovery), 'Invalid expired-task recovery list'
        boundary_recovery=scope.get('resume_boundary_task_ids',[])
        assert isinstance(boundary_recovery,list) and all(type(i) is int and i>0 for i in boundary_recovery) and len(set(boundary_recovery))==len(boundary_recovery), 'Invalid boundary-task recovery list'
        assert not set(boundary_recovery)&set(recovery), 'Overlapping recovery lists'
        budget_recovery=scope.get('resume_budget_task_ids',[])
        assert isinstance(budget_recovery,list) and all(type(i) is int and i>0 for i in budget_recovery) and len(set(budget_recovery))==len(budget_recovery), 'Invalid budget-task recovery list'
        assert not set(budget_recovery)&(set(recovery)|set(scope.get('resume_boundary_task_ids',[]))), 'Overlapping recovery lists'
        parked_recovery=scope.get('resume_parked_task_ids',[])
        assert isinstance(parked_recovery,list) and all(type(i) is int and i>0 for i in parked_recovery) and len(set(parked_recovery))==len(parked_recovery), 'Invalid parked-task recovery list'
        assert not set(parked_recovery)&(set(recovery)|set(boundary_recovery)|set(budget_recovery)), 'Overlapping recovery lists'
        if parked_recovery:
            assert scope.get('bounded_native_continuation') is True and scope.get('full_task_row_fingerprints'), 'Parked recovery needs an explicit frozen continuation'
        if recovery or budget_recovery or parked_recovery:
            assert selected is not None and type(scope.get('maximum_wall_seconds')) is int and 0<scope['maximum_wall_seconds']<=900, 'Expired recovery needs an explicit school and bounded additional time'
        protected=set()
        if scope.get('full_task_row_fingerprints'):
            path=(ROOT/scope['full_task_row_fingerprints']).resolve()
            assert path.is_relative_to(BASE.resolve()) and sha(path)==scope.get('full_task_row_fingerprints_sha256'), 'Prepared freeze certificate changed'
            certificate=read(path)
            admitted=set(recovery) | set(scope.get('resume_boundary_task_ids',[])) | set(budget_recovery) | set(parked_recovery)
            declared=certificate.get('admitted_task_ids',[])
            frozen=certificate.get('protected_task_ids',[])
            assert selected is not None and admitted and all(type(i) is int and i>0 for i in declared+frozen), 'Invalid freeze partition'
            protected=set(frozen)
            assert len(protected)==len(frozen) and len(set(declared))==len(declared) and set(declared)==admitted and not admitted & protected, 'Invalid freeze partition'
            actual={str(row['id']):hashlib.sha256(json.dumps(dict(row),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
                for row in connection.execute('SELECT * FROM background_tasks ORDER BY id')}
            assert admitted | protected=={int(i) for i in actual}, 'Prepared freeze partition does not cover all existing jobs'
            assert actual==certificate.get('all_existing_task_rows'), 'Prepared task row changed'
        found_recovery=set();found_budget_recovery=set();found_boundary_recovery=set();found_parked_recovery=set();jobs=[]
        for row in connection.execute('SELECT id,kind,generation,state,phase,error_code,capability,token,worker_id,lease_until,'
                'attempts,checkpoint,payload,available_at,deadline_at FROM background_tasks ORDER BY id'):
            row=dict(row)
            assert row['state'] in ('pending','waiting','done','failed') and not any(row[k] for k in ('token','worker_id','lease_until')), 'Native task has live ownership'
            if scope.get('model_free_reader_window') and row['id'] in set(recovery)|set(boundary_recovery):
                assert row['kind']=='discover', 'Repeated zero-model slices require the inventory reader'
            if row['id'] in parked_recovery:
                assert (row['state']=='pending' and row['phase'] in ('directory_slice','ai_resource','fetch')
                    and not row['error_code'] and row['kind'] in NATIVE_SAMPLE_KINDS
                    and str(json.loads(row['payload'] or '{}').get('school_id'))==str(selected)), 'Invalid parked native task'
                assert scope.get('original_target_deadlines',{}).get(str(row['id']))==row['deadline_at'], 'Missing original parked deadline'
                found_parked_recovery.add(row['id'])
            if row['id'] in boundary_recovery:
                assert (row['error_code']=='sampling_outside_declared_host'
                    and (row['state'],row['phase']) in (('pending','sampling_boundary'),('waiting','sampling_scope'))
                    and row['kind'] in NATIVE_SAMPLE_KINDS
                    and (selected is None or str(json.loads(row['payload'] or '{}').get('school_id'))==str(selected))), 'Invalid paused sampling-scope task'
                if row['state']=='waiting':
                    assert selected is not None and type(scope.get('maximum_wall_seconds')) is int and 0<scope['maximum_wall_seconds']<=900, 'Scope wait recovery needs an explicit school and bounded time'
                found_boundary_recovery.add(row['id'])
            if row['id'] in budget_recovery:
                assert (row['state']=='pending' and row['phase']=='sampling_boundary'
                    and row['error_code'] in ('sampling_document_budget','sampling_http_budget','sampling_model_budget','sampling_wall_budget')
                    and row['kind'] in NATIVE_SAMPLE_KINDS
                    and str(json.loads(row['payload'] or '{}').get('school_id'))==str(selected)), 'Invalid paused sampling-budget task'
                found_budget_recovery.add(row['id'])
            deadline=row.get('deadline_at')
            if deadline and row['state'] in ('pending','waiting'):
                stamp=datetime.fromisoformat(deadline)
                if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
                target=(selected is None or (row['kind'] in NATIVE_SAMPLE_KINDS
                    and str(json.loads(row['payload'] or '{}').get('school_id'))==str(selected))) and row['id'] not in protected
                if stamp<=datetime.now(timezone.utc) and target:
                    assert row['id'] in set(recovery)|set(budget_recovery)|set(boundary_recovery)|set(parked_recovery), 'Native deadline expired; explicit native recovery is required'
                    assert row['state']=='pending' or row['id'] in boundary_recovery, 'Native deadline expired; explicit native recovery is required'
                    if row['id'] in recovery:found_recovery.add(row['id'])
            for field in ('checkpoint','payload'):
                row[field+'_sha256']=hashlib.sha256((row.pop(field) or '{}').encode()).hexdigest()
            jobs.append(row)
        assert found_recovery==set(recovery), 'Recovery list includes a non-expired, finished or unrelated task'
        assert found_budget_recovery==set(budget_recovery), 'Budget recovery includes a missing task'
        assert found_boundary_recovery==set(boundary_recovery), 'Boundary recovery includes a missing task'
        assert found_parked_recovery==set(parked_recovery), 'Parked recovery includes a missing task'
        assert jobs, 'There is no native checkpoint to continue'
        assert any(row['state'] in ('pending','waiting') for row in jobs), 'No resumable native work remains'
        executions=[]
        for row in connection.execute('SELECT execution_id,purpose,status,profile_id,config_version,skill_digest,'
                'input_digest,request_sent,usage,output FROM ai_executions ORDER BY execution_id'):
            row=dict(row);usage=json.loads(row['usage'] or '{}')
            assert row['status'] not in ('reserved','sending','uncertain'), 'An earlier model result is unsettled'
            assert not row['request_sent'] or usage.get('known') is True, 'An earlier sent request has unknown usage'
            row['output_sha256']=hashlib.sha256((row.pop('output') or '').encode()).hexdigest()
            executions.append(row)
        budgets=[dict(r) for r in connection.execute('SELECT key,used_tokens,reserved_tokens,active_count,'
            'used_requests,reserved_requests FROM ai_budgets ORDER BY key')]
        assert all(not any(r[k] for k in ('reserved_tokens','active_count','reserved_requests')) for r in budgets), 'Model resources remain reserved'
        columns=[dict(r) for r in connection.execute('SELECT id,name,kind,list_url,list_selector,title_selector,'
            'link_selector,date_selector,content_selector,group_name FROM departments ORDER BY id')]
        limit=connection.execute("SELECT value FROM app_config WHERE key='ai_request_limit_school_discovery'").fetchone()
        state=dict(schools=schools,jobs=jobs,executions=executions,budgets=budgets,
            columns_sha256=hashlib.sha256(json.dumps(columns,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
            department_records=len(columns),native_school_request_limit=limit[0] if limit else None)
    state.update(model_counter=read(instance/'control/model-attempts.json'),
        cumulative_web_ledger_sha256=sha(instance/'control/pilot-web-ledger.json'))
    encoded=json.dumps(state,sort_keys=True,separators=(',',':')).encode()
    return dict(sha256=hashlib.sha256(encoded).hexdigest(),state=state)


def check_continuation(scope):
    anchor_path=(ROOT/scope['continuation_anchor']).resolve()
    assert anchor_path.is_relative_to(BASE.resolve())
    assert sha(anchor_path)==scope['continuation_anchor_sha256'], 'Prepared continuation anchor changed'
    manifest_path=(ROOT/scope['code_manifest']).resolve()
    assert manifest_path.is_relative_to(BASE.resolve()) and sha(manifest_path)==scope['code_manifest_sha256'], 'Prepared code manifest changed'
    for name,digest in read(manifest_path).items():
        path=(ROOT/name).resolve()
        assert path.is_relative_to(ROOT.resolve()) and sha(path)==digest, 'Code changed after continuation preparation'
    anchor=read(anchor_path)
    current=continuation_state(scope)
    assert current['sha256']==anchor['sha256'], 'Native state changed since the continuation was prepared'
    for name,digest in scope['retained_control_files'].items():
        path=(ROOT/name).resolve()
        assert path.is_relative_to(BASE.resolve()) and sha(path)==digest, 'Prior closed ledger changed'
    return current


def check_continuation_approval(scope_path,scope):
    path=(ROOT/scope['approval_record']).resolve()
    assert path.is_relative_to(BASE.resolve()) and path.exists(), 'Explicit approval of the additional scope is required'
    approval=read(path)
    assert approval.get('approved') is True and approval.get('scope_sha256')==sha(scope_path), 'Approval does not match this scope'


def check_added_school(scope):
    """Admit a fresh school without resetting or copying the configured instance."""
    instance=(ROOT/scope['instance_path']).resolve()
    assert instance.is_relative_to(BASE.resolve())
    assert sha(instance/'school_watcher.db')==scope['existing_business_sha256'], 'Configured instance changed; inspect before sampling'
    for relative,digest in scope['retained_control_files'].items():
        path=(ROOT/relative).resolve()
        assert path.is_relative_to(instance) and sha(path)==digest, 'Existing cumulative ledger changed'
    manifest_path=(ROOT/scope['code_manifest']).resolve()
    assert manifest_path.is_relative_to(BASE.resolve()) and sha(manifest_path)==scope['code_manifest_sha256']
    manifest=read(manifest_path)
    assert all(sha(ROOT/relative)==digest for relative,digest in manifest.items()), 'Prepared sampling code changed'
    connection=sqlite3.connect((instance/'school_watcher.db').as_uri()+'?mode=ro',uri=True)
    try:
        connection.execute('PRAGMA query_only=ON')
        schools=connection.execute('SELECT id,name,url FROM schools').fetchall()
        assert sorted(row[0] for row in schools)==scope['existing_school_ids'], 'School inventory changed'
        assert all(row[1]!=scope['school']['name'] and row[2]!=scope['school']['root_url'] for row in schools), 'Inspect existing school progress; no blind restart'
        assert connection.execute("SELECT count(*) FROM background_tasks WHERE state='running' OR worker_id IS NOT NULL OR lease_until IS NOT NULL").fetchone()[0]==0, 'Existing tasks have live ownership'
        assert connection.execute("SELECT count(*) FROM ai_executions WHERE status IN ('reserved','sending','uncertain')").fetchone()[0]==0, 'Earlier model results remain unsettled'
    finally:connection.close()


def school_tasks(school_id, *, native_only=False):
    from backend.database.models import BackgroundTask
    from backend.ai.school_budget import payload_school_id
    return [row for row in BackgroundTask.query.all()
            if str(payload_school_id(row.payload or {}))==str(school_id)
            and (not native_only or row.kind in NATIVE_SAMPLE_KINDS)]


@contextmanager
def school_queue(scope,school_id,*,preserved_task_ids=None):
    """Filter schools before the native claim limit; keep its within-school order."""
    if not scope.get('new_school_in_existing_instance') and not scope.get('continuation_school_id'):
        yield;return
    from unittest.mock import patch
    from backend.services import tasks
    from backend.database.models import BackgroundTask
    preserved=preserved_native_task_ids(scope) if preserved_task_ids is None else preserved_task_ids
    original=tasks._claim_statement
    def statement(eligible,at):
        selected={row.id for row in school_tasks(school_id,native_only=bool(scope.get('continuation_school_id')))}
        if scope.get('model_free_native_slice'):
            selected.intersection_update(set(scope.get('resume_expired_task_ids',[]))|
                                         set(scope.get('resume_boundary_task_ids',[])))
        return original(eligible & ~BackgroundTask.id.in_(preserved) & BackgroundTask.id.in_(selected),at)
    with patch.object(tasks,'_claim_statement',statement):yield


def preserved_native_task_ids(scope):
    """Preserve pre-existing tasks outside the explicitly resumed ID set."""
    if scope.get('continuation_school_id'):
        path=(ROOT/scope['continuation_anchor']).resolve()
        assert path.is_relative_to(BASE.resolve()) and sha(path)==scope['continuation_anchor_sha256']
        prepared_jobs=read(path)['state']['jobs']
        existing={job['id'] for job in prepared_jobs}
        admitted=set(scope.get('resume_expired_task_ids',[])) | set(scope.get('resume_boundary_task_ids',[])) | set(scope.get('resume_budget_task_ids',[])) | set(scope.get('resume_parked_task_ids',[]))
        # Current, unexpired pending work from the selected school may continue
        # when it was explicitly present in the prepared anchor.
        if not admitted:
            prepared_pending={job['id'] for job in prepared_jobs if job['state']=='pending'}
            admitted={job.id for job in school_tasks(scope['continuation_school_id'],native_only=True)
                if job.id in prepared_pending}
        return frozenset(existing-admitted)
    if scope.get('new_school_in_existing_instance'):
        from backend.database.models import BackgroundTask
        from backend.ai.school_budget import payload_school_id
        prior=scope.get('existing_school_ids',[])
        return frozenset(row.id for row in BackgroundTask.query.all()
            if payload_school_id(row.payload or {}) in prior)
    return frozenset()


def initialize_native_state(scope):
    """Continue the existing generation; never use enqueue as a resume shortcut."""
    from backend.database.db import db
    from backend.database.models import School,Department,Subscription,BackgroundTask,AppConfig
    from backend.services.school_registry import ensure_school
    from backend.services import tasks
    if scope.get('continuation_anchor'):
        state=check_continuation(scope)['state']
        if scope.get('continuation_school_id'):
            school=next(s for s in state['schools'] if s['id']==scope['continuation_school_id'])
        else:
            assert len(state['schools'])==1
            school=state['schools'][0]
        assert (school['name'],school['url'])==(scope['school']['name'],scope['school']['root_url'])
        try:
            for task_id in scope.get('resume_parked_task_ids',[]):
                from sqlalchemy import update
                original=next(j for j in state['jobs'] if j['id']==task_id)
                released_at=datetime.now(timezone.utc).replace(tzinfo=None)
                deadline=datetime.fromisoformat(original['deadline_at']) if original['deadline_at'] else released_at
                changed=db.session.execute(update(BackgroundTask).where(BackgroundTask.id==task_id,
                    BackgroundTask.state=='pending',BackgroundTask.generation==original['generation'],
                    BackgroundTask.phase==original['phase'],BackgroundTask.error_code==original['error_code'],
                    BackgroundTask.available_at==datetime.fromisoformat(original['available_at']),
                    BackgroundTask.deadline_at==(datetime.fromisoformat(original['deadline_at']) if original['deadline_at'] else None),
                    BackgroundTask.token.is_(None),BackgroundTask.worker_id.is_(None),BackgroundTask.lease_until.is_(None)
                ).values(available_at=released_at,deadline_at=max(deadline,released_at+timedelta(seconds=scope['maximum_wall_seconds']))))
                assert changed.rowcount==1,'Parked native task changed before explicit recovery'
            if scope.get('resume_expired_task_ids'):
                from sqlalchemy import case, update
                released_at=datetime.now(timezone.utc).replace(tzinfo=None)
                resumed_deadline=released_at+timedelta(seconds=scope['maximum_wall_seconds'])
                for task_id in scope['resume_expired_task_ids']:
                    original=next(j for j in state['jobs'] if j['id']==task_id)
                    changed=db.session.execute(update(BackgroundTask).where(BackgroundTask.id==task_id,
                        BackgroundTask.state=='pending',BackgroundTask.generation==original['generation'],
                        BackgroundTask.deadline_at==datetime.fromisoformat(original['deadline_at']),
                        BackgroundTask.token.is_(None),BackgroundTask.worker_id.is_(None),BackgroundTask.lease_until.is_(None)
                    ).values(deadline_at=resumed_deadline,available_at=case(
                        (BackgroundTask.available_at>released_at,released_at),
                        else_=BackgroundTask.available_at)))
                    assert changed.rowcount==1,'Native task changed before explicit recovery'
            for task_id in scope.get('resume_boundary_task_ids',[]):
                job=db.session.get(BackgroundTask,task_id,populate_existing=True)
                assert job and (job.state,job.phase) in (('pending','sampling_boundary'),('waiting','sampling_scope'))
                assert job.error_code=='sampling_outside_declared_host', 'Only the declared domain boundary may be released'
                original=next(j for j in state['jobs'] if j['id']==task_id)
                from sqlalchemy import update
                released_at=datetime.now(timezone.utc).replace(tzinfo=None)
                values=dict(state='pending',available_at=released_at)
                if job.deadline_at and job.deadline_at<=released_at:
                    values['deadline_at']=released_at+timedelta(seconds=scope['maximum_wall_seconds'])
                changed=db.session.execute(update(BackgroundTask).where(BackgroundTask.id==task_id,
                    BackgroundTask.generation==original['generation'],BackgroundTask.state==original['state'],
                    BackgroundTask.phase==original['phase'],BackgroundTask.error_code=='sampling_outside_declared_host',
                    BackgroundTask.token.is_(None),BackgroundTask.worker_id.is_(None),BackgroundTask.lease_until.is_(None)
                ).values(**values))
                assert changed.rowcount==1,'Native scope-wait task changed before explicit recovery'
            for task_id in scope.get('resume_budget_task_ids',[]):
                job=db.session.get(BackgroundTask,task_id,populate_existing=True)
                original=next(j for j in state['jobs'] if j['id']==task_id)
                assert job and job.state=='pending' and job.generation==original['generation']
                assert job.phase=='sampling_boundary' and job.error_code==original['error_code']
                released_at=datetime.now(timezone.utc).replace(tzinfo=None)
                job.available_at=released_at
                if job.deadline_at and job.deadline_at<=released_at:
                    job.deadline_at=released_at+timedelta(seconds=scope['maximum_wall_seconds'])
            # Budget changes only at the explicitly declared cumulative ceiling.
            # Used/reserved budgets, executions, sources and task generations stay.
            AppConfig.set('ai_request_limit_school_discovery',str(scope['native_school_request_limit']))
            db.session.commit()
        except BaseException:
            db.session.rollback();raise
        db.session.remove()
        return school['id']
    if scope.get('new_school_in_existing_instance'):
        assert sorted(row.id for row in School.query.all())==scope['existing_school_ids'], 'School inventory changed'
        assert not School.query.filter((School.name==scope['school']['name']) | (School.url==scope['school']['root_url'])).first(), 'Inspect existing school progress; no blind restart'
    else:
        assert School.query.count()==Department.query.count()==BackgroundTask.query.count()==0, 'Inspect prior progress; no blind restart'
    school,_=ensure_school(scope['school']['name'],scope['school']['root_url'],origin='isolated_native_sampling')
    owner=int(AppConfig.get('desktop_owner_id'))
    db.session.add(Subscription(user_id=owner,school_id=school.id));school.subscriber_count=1
    tasks.enqueue('discover',school.id,dict(school_id=school.id,trigger='background_discovery',ai_assist=True),commit=False)
    school_id=school.id
    db.session.commit();db.session.remove()
    return school_id


def validate_model_free_scope(scope):
    """Zero new calls is a hard cumulative cap, never an unlimited setting."""
    if not scope.get('model_free_native_slice'):
        return False
    model = scope['model']
    targets=scope.get('resume_expired_task_ids',[])+scope.get('resume_boundary_task_ids',[])
    invocations = scope.get('maximum_task_invocations')
    if scope.get('model_free_reader_window'):
        website = scope['website']
        if (type(invocations) is not int or not 1 <= invocations <= 8
                or type(scope.get('maximum_wall_seconds')) is not int or not 0 < scope['maximum_wall_seconds'] <= 480
                or type(scope.get('continuation_school_id')) is not int or scope['continuation_school_id'] <= 0
                or any(type(website.get(k)) is not int for k in ('existing_document_attempts','maximum_new_documents',
                    'maximum_documents','existing_http_attempts','maximum_new_http_attempts','maximum_http_attempts'))
                or website['existing_document_attempts'] < 0 or website['existing_http_attempts'] < 0
                or not 0 < website['maximum_new_documents'] <= 24 or not 0 < website['maximum_new_http_attempts'] <= 48
                or website['maximum_documents'] != website['existing_document_attempts'] + website['maximum_new_documents']
                or website['maximum_http_attempts'] != website['existing_http_attempts'] + website['maximum_new_http_attempts']):
            raise ValueError('A zero-model reader window needs one reader, at most eight slices,480seconds,24documents and48HTTP')
    elif invocations != 1:
        raise ValueError('A legacy model-free scope admits one task slice only')
    if (model.get('maximum_new_dispatch_attempts') != 0
            or model.get('maximum_dispatch_attempts') != model.get('existing_dispatch_attempts')
            or scope['website'].get('browser_calls') != 0
            or str(model.get('declared_cost_ceiling_cny')) != '0'
            or len(targets) != 1
            or type(scope.get('existing_max_task_id')) is not int
            or scope.get('http_only_validation')):
        raise ValueError('Model-free work needs zero new calls/cost/browser and one existing task slice')
    target=targets[0]
    if type(target) is not int or not 0<target<=scope['existing_max_task_id']:
        raise ValueError('Model-free work needs one explicitly declared existing task')
    datetime.fromisoformat(scope['original_target_deadline'])
    return True


def model_budget_reached(scope, used):
    if validate_model_free_scope(scope):
        if used != scope['model']['existing_dispatch_attempts']:
            raise ValueError('Prior model ledger changed; zero-call work cannot reset it')
        return False  # HTTP may proceed; Bounds still denies every model call.
    return used >= scope['model']['maximum_dispatch_attempts']


def native_child(scope_path,output,deadline):
    scope=read(scope_path);instance=(ROOT/scope['instance_path']).resolve();output=Path(output)
    require_model_admission(scope)
    for key in ('DATABASE_URL','FIELD_ENC_KEY','SECRET_KEY','WATCHER_DISCOVERY_POLICY','WATCHER_DISCOVERY_ATTENTION','WATCHER_BROWSER_TOKEN'):
        os.environ.pop(key,None)
    os.environ.update(WATCHER_DATA_DIR=str(instance),WATCHER_ENV_FILE=str(instance/'.env'),WATCHER_DESKTOP='1',
        WATCHER_SEED_ON_START='0',WATCHER_BROWSER='0',WATCHER_ENV='development')
    from backend import create_app
    from backend.database.db import db
    from backend.database.models import BackgroundTask
    from backend.services import tasks
    from backend.ai.configuration import get_model_binding
    from backend.ai.material_capture import capture_materials
    from backend.services.source_onboarding import preserve_onboarding_tasks
    from backend.worker import execute
    app=create_app(dict(DESKTOP_MODE=True,BROWSER_ENABLED=False))
    bounds=Bounds(scope,instance,output/'web-materials',deadline)
    reason='unknown';completed=[];school_id=None
    with app.app_context():
        preserved=preserved_native_task_ids(scope)
    save(output/'automatic-recovery-protection.json',dict(preserved_task_ids=sorted(preserved),
        rule='No automatic recovery/scheduling writes or claims for these pre-existing IDs'))
    with preserve_onboarding_tasks(preserved),bounds.install(),capture_materials(output/'execution-materials',label='bounded-real-native-development-pilot',
            max_bytes=128*1048576,max_record_bytes=8*1048576) as recording:
        try:
            with app.app_context():
                binding=get_model_binding('directory')
                assert binding['model']=='deepseek-flash'
                school_id=initialize_native_state(scope)
            with school_queue(scope,school_id,preserved_task_ids=preserved):
                while True:
                    bounds.check_time()
                    ledger=read(bounds.web_file)
                    if len(ledger['documents'])>=scope['website']['maximum_documents']:
                        reason='document_budget';break
                    if (not scope.get('http_only_validation') and model_budget_reached(scope,
                            read(instance/'control/model-attempts.json')['used'])):
                        reason='model_budget';break
                    with app.app_context():
                        capabilities=('http',) if scope.get('http_only_validation') else ('http','directory')
                        handle=tasks.claim(capabilities=capabilities,worker_id='declared-native-pilot')
                        if handle is None:
                            pending=(school_tasks(school_id,native_only=bool(scope.get('continuation_school_id'))) if (scope.get('new_school_in_existing_instance') or scope.get('continuation_school_id')) else
                                     BackgroundTask.query.all())
                            pending=[t for t in pending if t.state in ('pending','running','waiting')]
                            if not pending:reason='native_queue_exhausted';break
                            if not any(t.state=='pending' and t.capability in capabilities for t in pending):
                                reason='native_wait_requires_external_condition';break
                        db.session.remove()
                    if handle:
                        execute(app,handle)
                        completed.append(dict(task_id=handle['id'],generation=handle['generation']))
                        save(output/'progress.json',dict(completed_tasks=completed,at=now(),boundary=bounds.boundary))
                        if scope.get('complete_existing_http_task'):
                            assert scope.get('http_only_validation') and handle['id']==scope['complete_existing_http_task']
                            with app.app_context():
                                task=db.session.get(BackgroundTask,handle['id'],populate_existing=True)
                                if task.state in ('done','failed'):
                                    reason='existing_validation_task_finished';break
                                if task.attempts>scope['initial_failure_count']:
                                    reason='single_validation_retry_failed';break
                                if task.state=='waiting':
                                    reason='validation_requires_external_condition';break
                        if len(completed)>=scope.get('maximum_task_invocations',float('inf')):
                            reason='bounded_validation_attempt_finished';break
                    else:time.sleep(.5)
                    if bounds.boundary:reason=bounds.boundary;break
        except SamplingBoundary as error:reason=str(error)
        finally:
            if scope.get('model_free_native_slice') and school_id is not None:
                # The only admitted old reader and its new children keep their
                # IDs/checkpoints; no queued successor can run outside this scope.
                with app.app_context():
                    from datetime import timedelta
                    stamp=datetime.now(timezone.utc).replace(tzinfo=None)
                    ids=set(scope.get('resume_expired_task_ids',[]))|set(scope.get('resume_boundary_task_ids',[]))
                    for row in school_tasks(school_id,native_only=True):
                        if row.id in ids or row.id>scope['existing_max_task_id']:
                            if row.state in ('pending','waiting') and not row.token:
                                row.available_at=stamp+timedelta(days=1)
                                if row.id in ids:
                                    row.deadline_at=datetime.fromisoformat(scope['original_target_deadline'])
                    db.session.commit();db.session.remove()
            save(output/'child-outcome.json',dict(reason=reason,completed_tasks=completed,at=now(),
                certified_branch_episodes=0,threshold_gate='insufficient_evidence_no_evaluator'))
    save(output/'capture-status.json',dict(path=str(recording.path),complete=recording.complete,
        meaning='Recording completeness only; not certified website or branch coverage'))


def bounded_process(target,args,seconds):
    process=multiprocessing.get_context('spawn').Process(target=target,args=args)
    started=time.monotonic();process.start()
    while process.is_alive() and time.monotonic()-started<seconds:
        process.join(min(5,max(0,seconds-(time.monotonic()-started))))
    hard_stopped=process.is_alive()
    if hard_stopped:process.terminate();process.join(5)
    assert not process.is_alive(),'Owned sampling child did not stop'
    return dict(pid=process.pid,exit_code=process.exitcode,hard_stopped=hard_stopped,
        elapsed_seconds=round(time.monotonic()-started,6))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope',type=Path,default=BASE/'deepseek-development-pilot-scope-20261026.json')
    parser.add_argument('--certificate',type=Path,default=BASE/'native-pilot-preflight-20261026.json')
    parser.add_argument('--run',action='store_true')
    parser.add_argument('--check-only',action='store_true')
    options=parser.parse_args()
    scope_path=options.scope.resolve();scope=read(scope_path)
    assert scope_path.is_relative_to(BASE.resolve())
    state=metadata(scope)
    if not options.run:
        continuation=check_continuation(scope) if scope.get('continuation_anchor') else None
        print(json.dumps(dict(scope_sha256=sha(scope_path),driver_sha256=sha(__file__),configuration=state,
            resource_limits=scope['website'],model_max_attempts=scope['model']['maximum_dispatch_attempts'],
            wall_seconds=scope['maximum_wall_seconds'],continuation_anchor_matches=bool(continuation),
            no_sampling_started=True),ensure_ascii=False));return
    certificate_path=options.certificate.resolve()
    assert certificate_path.is_relative_to(BASE.resolve())
    certificate=read(certificate_path)
    assert certificate['scope_sha256']==sha(scope_path) and certificate['driver_sha256']==sha(__file__) and certificate['safety_checks_passed']
    if scope.get('approval_record'):check_continuation_approval(scope_path,scope)
    assert state['directory_binding_ready'],'Independent tested directory binding is required'
    require_model_admission(scope)
    assert datetime.now(timezone.utc)<datetime.fromisoformat(scope['expires_at']),'Review the expired scope before dispatch'
    instance=ROOT/scope['instance_path']
    from filelock import FileLock
    with FileLock(str(instance/'control/pilot-run.lock'),timeout=0):
        output=(instance/scope.get('output_directory','pilot-first-run')).resolve()
        assert output.parent==instance.resolve(), 'Output must be a direct child of this instance'
        assert not output.exists(),'Inspect the existing process/outcome; never restart from a status timeout'
        if scope.get('continuation_anchor'):check_continuation(scope)
        if scope.get('new_school_in_existing_instance'):check_added_school(scope)
        output.mkdir()
        # This driver reads no reference targets or future graph to seed the native queue.
        files=sorted((ROOT/'backend').rglob('*.py'))
        save(output/'frozen-code.json',{p.relative_to(ROOT).as_posix():sha(p) for p in files})
        initial=scope['school'] if not scope.get('continuation_anchor') else dict(native_continuation_anchor=scope['continuation_anchor'])
        save(output/'initial-state.json',dict(scope_sha256=sha(scope_path),configuration=state,initial_policy_input=initial,
            imported_sources=0,target_annotations_loaded=False,started_at=now()))
        result=bounded_process(native_child,(str(scope_path),str(output),time.monotonic()+scope['maximum_wall_seconds']),scope['maximum_wall_seconds'])
        result.update(ended_at=now(),certified_branch_episodes=0,threshold_gate='insufficient_evidence_no_evaluator',
            interrupted_response_policy='Preserve durable sending/unknown usage; no automatic retry or zero-cost inference')
        save(output/'process-outcome.json',result)
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
