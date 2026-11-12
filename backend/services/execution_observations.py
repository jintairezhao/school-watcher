"""Reference existing AI invoices from actual result uses; never bill or infer IO.

Observation is opt-in and scoped to the executing caller. No observer commits,
retries a request, reads prompts/output, or creates a source/organization fact.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import logging
import re

_observer=ContextVar('model_use_observer',default=None)
_invocation=ContextVar('model_use_invocation',default=None)
HEX=re.compile(r'[0-9a-f]{64}')
IDENTITY=re.compile(r'[a-z][a-z_-]{0,40}:[0-9a-f]{64}')
LEDGER_FIELDS=('execution_id','input_digest','skill_digest','purpose','mode','profile_id','config_version')
REF_FIELDS=LEDGER_FIELDS+('school_id',)


def reference(value):
    if not isinstance(value,dict) or not isinstance(value.get('execution_id'),str):return None
    if any(key not in value for key in REF_FIELDS):return None
    if not IDENTITY.fullmatch(value['execution_id']):return None
    if any(not isinstance(value.get(key),str) or not HEX.fullmatch(value[key]) for key in ('input_digest','skill_digest')):return None
    if value.get('purpose') not in ('directory','summary'):return None
    if type(value.get('school_id')) is not int or value['school_id']<=0:return None
    if value.get('mode') not in ('navigation','classify','extraction','column','summary','assess'):return None
    if value.get('profile_id') is not None and (type(value['profile_id']) is not int or value['profile_id']<=0):return None
    if not isinstance(value.get('config_version'),str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',value['config_version']):return None
    return {key:value[key] for key in REF_FIELDS}


def public_uses(values):
    output=[]
    for value in values[:64] if isinstance(values,list) else []:
        ref=reference(value)
        if not ref:continue
        kind=value.get('use_kind')
        if kind not in ('request_attempt','ledger_reuse','no_dispatch'):continue
        output.append(dict(ref,use_kind=kind,observed_status=value.get('observed_status')
            if value.get('observed_status') in ('reserved','sending','succeeded','partial','failed','uncertain') else 'unknown'))
    return output


def public_cache_uses(values):
    output=[]
    for value in values[:64] if isinstance(values,list) else []:
        if not isinstance(value,dict) or not isinstance(value.get('cache_key'),str) or not HEX.fullmatch(value['cache_key']):continue
        output.append(dict(cache_key=value['cache_key'],execution_reference=reference(value.get('execution_reference'))))
    return output


@contextmanager
def observe_model_uses():
    observation=dict(uses=[],cache_reuses=[],unrecorded_attempts=0)
    marker=_observer.set(observation)
    try:yield observation
    finally:_observer.reset(marker)


def execution_identity(execution_id,*,reused=False,dispatched=False):
    current=_invocation.get()
    if current is not None:
        current.update(execution_id=execution_id,reused=current.get('reused',False) or reused,
            dispatched=current.get('dispatched',False) or dispatched)


def observe_runtime(function):
    """Read a verified ledger reference after an actual runtime invocation."""
    @wraps(function)
    def call(*args,**kwargs):
        observation=_observer.get()
        if observation is None:return function(*args,**kwargs)
        invocation=dict(execution_id=kwargs.get('execution_id'),reused=False,dispatched=False)
        marker=_invocation.set(invocation)
        try:return function(*args,**kwargs)
        finally:
            try:
                from backend.ai.models import AIExecution
                from backend.database.db import db
                fields=LEDGER_FIELDS+('status','diagnostics')
                # Runtime deliberately releases its transaction before returning.
                # This observation must not reopen the caller's session or flush it.
                with db.engine.connect() as connection:
                    row=connection.execute(db.select(*(getattr(AIExecution,key) for key in fields)).where(
                        AIExecution.execution_id==invocation['execution_id'])).first()
                saved=dict(zip(fields,row)) if row else {}
                diagnostics=saved.get('diagnostics')
                context=diagnostics.get('discovery_context') if isinstance(diagnostics,dict) else None
                context=context if isinstance(context,dict) else {}
                saved['school_id']=context.get('school_id')
                binding=kwargs.get('binding') or {}
                expected={key:kwargs.get(key) for key in ('input_digest','skill_digest','purpose','mode')}
                expected.update(profile_id=binding.get('id') or None,config_version=str(binding.get('version')))
                expected['school_id']=kwargs.get('school_id')
                ref=reference(saved)
                if ref and all(ref[key]==value for key,value in expected.items()):
                    if len(observation['uses'])<64:
                        observation['uses'].append(dict(ref,observed_status=saved['status'],use_kind=
                            'request_attempt' if invocation['dispatched'] else 'ledger_reuse' if invocation['reused'] else 'no_dispatch'))
                    else:observation['unrecorded_attempts']+=1
                else:observation['unrecorded_attempts']+=1
            except Exception as error:
                # An invoice/lease exception must never be hidden by observation.
                observation['unrecorded_attempts']+=1
                logging.getLogger(__name__).warning('Model reference observation unavailable: %s',type(error).__name__)
            finally:_invocation.reset(marker)
    return call


def execution_reference(result):
    observation=_observer.get()
    identity=(result.get('provenance') or {}).get('execution_id') if isinstance(result,dict) else None
    return next((reference(row) for row in reversed(observation['uses']) if row['execution_id']==identity),None) if observation else None


def record_cache_use(cache_key,row):
    observation=_observer.get()
    if observation is not None:
        if len(observation['cache_reuses'])<64:
            observation['cache_reuses'].append(dict(cache_key=cache_key,execution_reference=reference(row.get('execution_reference'))))
        else:observation['unrecorded_attempts']+=1


def capture_ledger(events,configurations,*,school_id):
    """Called by the shared snapshot reader; copy metadata, never model IO."""
    from backend.ai.models import AIExecution
    from backend.database.db import db
    retained=[]
    for row in configurations:
        payload=row.get('execution_json');receipt=payload.get('page_receipt') if isinstance(payload,dict) else None
        values=receipt.get('events') if isinstance(receipt,dict) else None
        if isinstance(values,list):retained.extend(event for event in values[:256] if isinstance(event,dict))
    identities=set()
    for event in [*events,*retained]:
        refs=[*public_uses(event.get('model_uses')),*(row['execution_reference'] for row in public_cache_uses(event.get('cache_reuses')))]
        identities.update(ref['execution_id'] for ref in refs if ref)
    # The event windows/receipts are already bounded. Chunk SQL parameters too.
    fields=LEDGER_FIELDS+('status','request_sent','usage','diagnostics')
    output=[];identities=sorted(identities)
    for start in range(0,len(identities),400):
        for row in db.session.query(*(getattr(AIExecution,key) for key in fields)).filter(
                AIExecution.execution_id.in_(identities[start:start+400])):
            record=dict(zip(fields,row))
            diagnostics=record.pop('diagnostics')
            context=diagnostics.get('discovery_context') if isinstance(diagnostics,dict) else None
            context=context if isinstance(context,dict) else {}
            if context.get('school_id')==school_id:
                output.append(deepcopy(dict(record,school_id=school_id)))
    return output


def project_ledger(events,rows):
    """Current ledger status is separate from the immutable observation time."""
    by_id={row['execution_id']:row for row in rows};unique={};all_ids=set()
    for event in events:
        receipts=[]
        uses=[(ref,ref['use_kind']) for ref in public_uses(event.get('model_uses'))]
        cache=public_cache_uses(event.get('cache_reuses'))
        uses.extend((item['execution_reference'],'shared_cache_reuse') for item in cache if item['execution_reference'])
        for ref,kind in uses:
            all_ids.add(ref['execution_id'])
            row=by_id.get(ref['execution_id'])
            valid=bool(row and all(row.get(key)==ref[key] for key in REF_FIELDS))
            usage=row.get('usage') if valid and isinstance(row.get('usage'),dict) else {}
            status=row['status'] if valid and row['status'] in ('reserved','sending','succeeded','partial','failed','uncertain') else 'unknown'
            known=bool(valid and row.get('request_sent') is True and status not in ('reserved','sending','unknown')
                and usage.get('known') is True and type(usage.get('total_tokens')) is int and usage['total_tokens']>=0)
            receipt=dict(execution_id=ref['execution_id'],use_kind=kind,status=status,
                ledger_verified=valid,request_sent=bool(row.get('request_sent')) if valid else None,
                usage_status='not_dispatched' if valid and not row.get('request_sent') else
                    'administrator_reconciled' if known and usage.get('source')=='administrator_reconciliation' else
                    'confirmed' if known else 'pending' if status in ('reserved','sending') else 'unknown',
                total_tokens=usage['total_tokens'] if known else None)
            receipts.append(receipt)
            if valid:unique[ref['execution_id']]=receipt
        if event.get('event_type')=='navigation_model_observed':
            event['model_usage']=dict(executions=receipts,shared_cache_reuses=len(cache),
                cache_origin_unknown=sum(item['execution_reference'] is None for item in cache),
                unrecorded_attempts=event.get('unrecorded_model_attempts',0))
    confirmed=[row for row in unique.values() if row['total_tokens'] is not None and row['request_sent']]
    return dict(scope='unique_ledger_references_in_published_event_window_not_incremental_cost',
        referenced_executions=len(all_ids),verified_executions=len(unique),unverified_references=len(all_ids)-len(unique),
        confirmed_tokens=sum(row['total_tokens'] for row in confirmed),
        unknown_usage=sum(row['request_sent'] is True and row['total_tokens'] is None
            and row['status'] not in ('reserved','sending') for row in unique.values()),
        pending_executions=sum(row['status'] in ('reserved','sending') for row in unique.values()))
