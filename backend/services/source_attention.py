"""Shared, evidence-scoped interests for scheduling and presentation only.

No new vocabulary, stopping rule, source eligibility, or ownership inference.
The existing attention function remains the sole scoring implementation.
"""
from copy import deepcopy
import hashlib
import json

from backend.scraper.discovery.attention import CATEGORY_KEYS, candidate_attention
from backend.services.structure_facts import _url


def policy_snapshot(policy):
    info = candidate_attention({}, profiles=policy.get('profiles'), weights=policy.get('weights'))
    from backend.scraper.discovery.parser_revision import PARSER_REVISION
    data = dict(profiles=list(info['profiles']), weights={key:float((policy.get('weights') or {}).get(key,1)) for key in CATEGORY_KEYS},
                rules_revision=PARSER_REVISION)
    version = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(data, version='attention-1:'+version)


def _feed(value):
    if isinstance(value, dict):return value
    try:
        decoded=json.loads(value or '{}')
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError):return {}


def material_attention(page, policy, configs=()):
    # path_json is a legacy display context, not established upstream evidence.
    local=dict(page, path_json='[]', path=[])
    info=candidate_attention(local, profiles=policy.get('profiles'), weights=policy.get('weights'))
    gains=dict(info['gains']);evidence=list(info['evidence']);needs=set(info['needs'])
    for config in configs:
        address=_url(page.get('final_url') or page.get('url'))
        if _url(config.get('list_url')) != address:continue
        target=_url(config.get('column_url'))
        if target and target != address:continue
        observed=dict(local, url=address, label=config.get('name') or '', title='', feed_json={
            'name':config.get('name') or '', 'container_locator':config.get('container_locator') or config.get('list_selector'),
            'samples':[dict(title=title) for title in config.get('sample_titles', [])]})
        part=candidate_attention(observed, profiles=policy.get('profiles'), weights=policy.get('weights'))
        needs.update(part['needs'])
        for category,value in part['gains'].items():gains[category]=max(gains.get(category, 0),value)
        for proof in part['evidence']:
            if proof not in evidence:evidence.append(proof)
    return dict(score=sum(gains.values()), needs=sorted(needs), gains=gains, evidence=evidence,
                reason='observed_demand' if gains else 'unclassified_or_other_interest_retained')


def onboarding_order(report, by_url, policy):
    frozen=policy_snapshot(policy)
    events=report.get('exploration_events', [])
    by_id={event['id']:event for event in events}
    completed={}
    for event in events:
        if event.get('event_type')!='page_inspection_finished' or event.get('state')!='fetched':continue
        key=(event.get('url'),event.get('content_hash'))
        if key not in completed or event['sequence']>completed[key]['sequence']:completed[key]=event
    traces={};ordered=[]
    from backend.services.source_execution_events import capture_page_receipt
    for position,page in enumerate(report['pages']):
        url=_url(page.get('final_url') or page.get('url'))
        info=material_attention(page,policy,by_url.get(url, ()))
        latest=completed.get((page['url'],page.get('content_hash')), {})
        parent=by_id.get(latest.get('parent_event_id'), {})
        traces[page['url']]=dict(schema_version=1, scope='page_onboarding_order', policy=frozen, **info,
            material=dict(reference_url=_url(page['url']), final_url=url, content_hash=page.get('content_hash'),
                          checked_at=page.get('checked_at'), parser_revision=page.get('parser_revision')),
            inspection_event_id=latest.get('id'), selection_event_id=parent.get('id'),
            inspection_receipt=capture_page_receipt(by_id,latest.get('id'),report.get('site',{}).get('site_key')),
            selection_context=deepcopy(parent.get('selection_context')),
            history_status='recorded' if latest else 'not_recorded', establishes_ownership=False)
        ordered.append((-info['score'],position,page))
    return [page for _,_,page in sorted(ordered,key=lambda item:item[:2])],traces


def validation_context(payload, html):
    original=payload.get('discovery_attention')
    if not isinstance(original,dict) or original.get('scope')!='page_onboarding_order':return None
    context=deepcopy(original)
    actual=hashlib.sha256(html.encode()).hexdigest()
    context.update(validation_material_hash=actual,
        scheduling_material_matches_validation=actual==context.get('material',{}).get('content_hash'),
        establishes_ownership=False)
    return context


def source_attention(node, config, report, policy, *, pages_by_url=None):
    """A shared page's other modules cannot lend interest to this source."""
    address=_url(node.get('url'))
    if pages_by_url is None:
        pages_by_url={url:page for page in report.get('pages', []) if (url:=_url(page.get('url')))}
    page=pages_by_url.get(address, {}) if address else {}
    value=_feed(page.get('feed_json'))
    columns=value.get('lists', [value]);columns=columns if isinstance(columns,list) else []
    matching=[column for column in columns if column.get('list_selector') and column['list_selector']==config.get('list_selector')]
    local=dict(page,url=address,label=node.get('label') or '',title='',path_json='[]',feed_json=None)
    configs=[dict(column,list_url=address,name=column.get('name') or node.get('label'),
        sample_titles=[sample.get('title','') for sample in column.get('samples',[])]) for column in matching]
    return material_attention(local,policy,configs)
