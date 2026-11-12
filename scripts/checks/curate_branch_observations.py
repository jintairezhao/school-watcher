"""Verify recorded collection provenance; no strategy, evaluator or acquisition."""
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import argparse
import gzip
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


def fingerprint(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def native_acquisition_observations(documents, events, *, capture_id):
    """Link raw HTTP observations to final results by the exact action only.

    A transport's default needs_adapter value is not the coordinator's verdict.
    Missing final observations stay unknown; neither layer validates a source.
    """
    by_action = {}
    for event in events:
        if event.get('event') != 'acquisition_result':
            continue
        payload = event['payload']
        if event.get('payload_sha256') != fingerprint(payload):
            raise ValueError('Coordinator observation fingerprint mismatch')
        ref = event.get('action_reference') or {}
        if ref.get('capture_id') != capture_id or not ref.get('action_id'):
            continue
        if payload.get('scope') != 'coordinator_result_not_http_dispatch_or_source_success':
            raise ValueError('Unknown coordinator observation scope')
        key = ref['action_id']
        if key in by_action:
            raise ValueError('Multiple coordinator results for one recorded action')
        by_action[key] = event
    rows = []
    for document in documents:
        ref = document.get('action_reference') or {}
        event = by_action.get(ref.get('action_id')) if ref.get('capture_id') == capture_id else None
        if event is not None and event['action_reference'] != ref:
            raise ValueError('Recorded action has conflicting decision context')
        raw = document.get('result')
        transport = ({key: raw.get(key) for key in ('final_url','status','outcome','error_code','transport')}
                     if raw is not None else None)
        if transport is not None:
            transport['content_hash'] = raw.get('decoded_html_sha256')
        payload = event['payload'] if event is not None else None
        coordinator = ({key: payload.get(key) for key in
            ('final_url','status','outcome','error_code','transport','content_hash')}
            if payload is not None else None)
        rows.append(dict(sequence=document['sequence'], requested_url=document['request']['url'],
            action_reference=ref or None,
            transport_recording_stage=document.get('observation_stage') or 'legacy_unlabelled_sampler_transport',
            transport_observation=transport, coordinator_observation=coordinator,
            coordinator_event_ref=event.get('_ref') if event is not None else None,
            classification_status='recorded_coordinator_result' if event is not None else 'not_recorded_unknown',
            source_validation_not_inferred=True))
    return rows


def address(value):
    part=urlsplit(value)
    if part.scheme not in ('http','https') or not part.hostname or part.username or part.password:
        raise ValueError('Invalid recorded public-page address')
    return urlunsplit((part.scheme,part.netloc.lower(),part.path or '/',part.query,''))


def stamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('Recorded acquisition time needs a timezone')
    return parsed.astimezone(timezone.utc)


def curate_logs(*,plan,decisions,observations,attempts,raw_reader):
    """Derive only what the historical decisions and original responses prove."""
    if len(decisions)!=len(observations) or not decisions:
        raise ValueError('A complete recorded decision is required for every observation')
    roots={school['id']:address(school['root_url']) for school in plan['schools']}
    run_id=fingerprint(dict(plan=plan,decisions=decisions,observations=observations,attempts=attempts))[:20]
    known={};soups={};steps=[];groups={};used_attempts=set()

    def soup_for(observation):
        digest=observation.get('body_sha256')
        if not digest:raise ValueError('Selected parent has no original response material')
        if digest not in soups:
            raw=raw_reader(observation['body_file'])
            if sha256(raw).hexdigest()!=digest:raise ValueError('Original response fingerprint mismatch')
            soups[digest]=BeautifulSoup(raw.decode(observation.get('detected_encoding') or 'utf-8'),'lxml')
        return soups[digest]

    def parent_for(candidate,sid,decided_at):
        source=candidate.get('source')
        if not source:
            if candidate.get('kind')!='root' or address(candidate['url'])!=roots[sid]:
                raise ValueError('An unobserved URL cannot become a root seed')
            return None
        key=(sid,address(source['source_url']),source['content_hash'],source.get('observed_at'))
        parent=known.get(key)
        if parent is None or stamp(parent['observation']['observed_at'])>decided_at:
            raise ValueError('Decision uses future or unknown parent material')
        observation=parent['observation']
        if source.get('observed_at')!=observation['observed_at']:
            raise ValueError('Candidate observation time differs from its actual parent')
        links=observation.get('analysis',{}).get('links',[])
        if not any(link.get('collection_decision')=='eligible_navigation'
                   and address(link['url'])==address(candidate['url']) and link.get('locator')==source.get('locator') for link in links):
            raise ValueError('Candidate was absent from the recorded parent links')
        return parent

    def witness(candidate,parent):
        source=candidate['source'];observation=parent['observation'];soup=soup_for(observation)
        node=soup.select_one(source['locator'])
        if node is None or not node.get('href'):raise ValueError('Selected link has no original DOM anchor')
        base=soup.find('base',href=True)
        resolved=address(urljoin(urljoin(observation['final_url'],base['href']) if base else observation['final_url'],node['href']))
        if resolved!=address(candidate['url']):raise ValueError('Selected DOM anchor targets a different page')
        ancestors=[node,*node.parents]
        context='header' if any(n.name=='header' for n in ancestors) else 'footer' if any(n.name=='footer' for n in ancestors) else 'content'
        return dict(parent_step=parent['step']['step'],reference_url=observation['final_url'],
            content_hash=observation['body_sha256'],observed_at=observation['observed_at'],
            locator=source['locator'],target_url=resolved,context=context)

    for number,(decision,observation) in enumerate(zip(decisions,observations),1):
        if decision['step']!=number or observation['sequence']!=number or decision['school_id']!=observation['school_id']:
            raise ValueError('Decision/observation sequence does not agree')
        sid=decision['school_id'];selected=decision['selected'];frontier=decision['known_frontier']
        if sid not in roots or not frontier or frontier[0]!=selected or selected!=observation['entry']:
            raise ValueError('Selected entry differs from the frozen known frontier')
        decided=stamp(decision['decided_at']);started=stamp(observation['started_at']);finished=stamp(observation['observed_at'])
        if not decided<=started<=finished:raise ValueError('Recorded visibility/acquisition clock order is inconsistent')
        for candidate in frontier:parent_for(candidate,sid,decided)
        parent=parent_for(selected,sid,decided)
        proof=witness(selected,parent) if parent else None
        # Header/footer links start a new navigation context. They do not turn
        # the page that happened to display them into an organizational parent.
        group_id=None
        if parent:
            group_id=parent['step']['navigation_group_id']
            if group_id is None or proof['context'] in ('header','footer'):
                group_id=f'{run_id}:navigation-entry:{number}'
                groups[group_id]=dict(id=group_id,entry_step=number,label=selected['label'],url=selected['url'],
                    scope_basis='Observed entry context; business/topic branch scope awaits independent annotation',steps=[])
        alternatives=[]
        for candidate in frontier:
            if address(candidate['url'])!=address(selected['url']) or not candidate.get('source'):continue
            alternative=witness(candidate,parent_for(candidate,sid,decided))
            if alternative not in alternatives:alternatives.append(alternative)
        indices=[]
        for position,attempt in enumerate(attempts):
            if attempt['school_id']==sid and started<=stamp(attempt['started_at'])<=stamp(attempt['finished_at'])<=finished:
                if position in used_attempts:raise ValueError('An HTTP attempt is allocated twice')
                indices.append(position);used_attempts.add(position)
        if not indices:raise ValueError('Actual request costs cannot be linked to this observation')
        owned=[attempts[position] for position in indices]
        if address(owned[0]['url'])!=address(observation['requested_url']):raise ValueError('First request is not the recorded selected page')
        if address(owned[-1]['url'])!=address(observation['final_url']):raise ValueError('Final request is not the recorded response')
        seconds=[item.get('seconds') for item in owned]
        if any(not isinstance(value,(int,float)) or value<0 for value in seconds):raise ValueError('Missing actual request duration')
        status=observation.get('status');failure=observation.get('failure')
        analysis=observation.get('analysis') or {}
        parser_outcome=analysis.get('outcome')
        # A successful HTTP response can still be an access challenge or an
        # unfinished page. Retain the recorded parser result, without rerunning it.
        html_outcome=parser_outcome if parser_outcome and parser_outcome!='usable' else 'html_observed'
        outcome=('acquisition_failed' if failure else 'http_access_restricted' if status in (401,403,451)
                 else 'http_not_found' if status==404 else html_outcome if status==200 else 'other_http_response')
        step=dict(step=number,school_id=sid,selected_url=selected['url'],label=selected['label'],
            decision_ref=dict(file='collection-decisions.json',index=number-1,fingerprint=fingerprint(decision)),
            known_observation_steps=sorted({item['step']['step'] for item in known.values()}),
            known_frontier_size=len(frontier),decided_at=decision['decided_at'],
            selected_parent_step=parent['step']['step'] if parent else None,
            recorded_discovery_ancestors=([*parent['step']['recorded_discovery_ancestors'],parent['step']['step']] if parent else []),
            navigation_group_id=group_id,selected_link_witness=proof,known_alternative_entries=alternatives,
            observation_ref=dict(file='observations.json',index=number-1,body_file=observation.get('body_file'),
                body_sha256=observation.get('body_sha256'),observed_at=observation['observed_at']),
            technical_outcome=outcome,http_status=status,failure=failure,semantic_value=None,
            original_parser_outcome=parser_outcome,original_parser_error_code=analysis.get('error_code'),
            original_parser_evidence=analysis.get('evidence',[]),
            cost=dict(http_attempt_sequences=[item['sequence'] for item in owned],http_attempts=len(owned),
                measured_request_seconds=round(sum(seconds),6),
                decoded_response_bytes=sum(item.get('body_bytes',0) for item in owned),
                attempts_with_unknown_response_bytes=sum('body_bytes' not in item for item in owned)))
        steps.append(step)
        if group_id:groups[group_id]['steps'].append(number)
        if observation.get('body_sha256'):
            # Verify every retained response, including HTTP failures.
            soup_for(observation)
            for url in (observation['requested_url'],observation['final_url']):
                known[(sid,address(url),observation['body_sha256'],observation['observed_at'])]=dict(step=step,observation=observation)
    if len(used_attempts)!=len(attempts):raise ValueError('Some recorded HTTP attempts have no observation owner')
    for group in groups.values():
        group['observed_later_visits_after_entry']=len(group['steps'])-1
        group['reviewed_business_branch_scope']=None
        group['semantic_result']=None
        group['continuation_coverage']='Finite observed visits only; unvisited descendants remain unknown'
        for number in group['steps']:
            steps[number-1]['later_same_navigation_group_steps']=[n for n in group['steps'] if n>number]
            steps[number-1]['later_recorded_descendant_steps']=[row['step'] for row in steps
                if row['step']>number and row['navigation_group_id']==group['id'] and number in row['recorded_discovery_ancestors']]
    return dict(schema_version=1,run_id=run_id,steps=steps,navigation_group_candidates=list(groups.values()),
        summary=dict(recorded_decisions=len(steps),original_responses=len(observations),actual_http_attempts=len(attempts),
            observed_navigation_group_candidates=len(groups),
            technical_outcomes={key:sum(s['technical_outcome']==key for s in steps) for key in sorted({s['technical_outcome'] for s in steps})},
            temporal_parent_visibility_checked=True,selected_dom_links_checked=True,frontier_parent_provenance_checked=True,
            costs_allocated_to_actual_observations=True,certified_branch_episodes=0,
            threshold_gate='insufficient_evidence_no_evaluator'),
        limitations=['Navigation context groups are curation candidates, not administrative hierarchy or independently reviewed business branches.',
            'A sibling visit does not establish continued exploration of the selected page subtree.',
            'Technical failures retain unknown semantic value; absent targets and unvisited pages are not negative labels.',
            'This tool does not select a strategy, simulate missing pages, test stopping criteria, install sources or recommend thresholds.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=Path(__file__).resolve().parents[2]
    run=args.run.resolve();output=args.output.resolve()
    if not run.is_relative_to(root) or not output.is_relative_to(root):raise ValueError('Curation inputs and outputs must stay in this workspace')
    read=lambda name:json.loads((run/name).read_text(encoding='utf-8-sig'))
    names=['scope.json','collection-decisions.json','observations.json','http-attempts.json','remaining-frontiers.json','frozen-code.json','summary.json','artifact-hashes.json']
    before={name:sha256((run/name).read_bytes()).hexdigest() for name in names}
    for name,digest in read('artifact-hashes.json').items():
        path=(run/name).resolve()
        if path.parent!=run or sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('The original run artifact fingerprint no longer matches')
    def raw_reader(name):
        path=(run/name).resolve()
        if path.parent!=run:raise ValueError('Raw response path leaves the recorded run')
        raw=gzip.decompress(path.read_bytes())
        if len(raw)>8*1024*1024:raise ValueError('Raw response exceeds the curation bound')
        return raw
    result=curate_logs(plan=read('scope.json')['plan'],decisions=read('collection-decisions.json'),
        observations=read('observations.json'),attempts=read('http-attempts.json'),raw_reader=raw_reader)
    result['input_record']=run.relative_to(root).as_posix();result['input_hashes']=before
    result['original_run_artifact_hashes_checked']=True
    result['original_run_summary']=read('summary.json');result['original_frozen_code']=read('frozen-code.json')
    result['remaining_frontier_counts']={key:len(value) for key,value in read('remaining-frontiers.json').items()}
    assert before=={name:sha256((run/name).read_bytes()).hexdigest() for name in names}
    output.mkdir(parents=True,exist_ok=False)
    (output/'trace.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    annotation=dict(schema_version=1,raw_trace='trace.json',reviewer=None,review_time=None,
        groups=[dict(navigation_group_id=g['id'],reviewed_business_branch_scope=None,important_targets=[],
            enough_continued_exploration=None,validated_source_identities=None,independent_holdout_qualification=None,
            semantic_result=None) for g in result['navigation_group_candidates']],
        certified_branch_episodes=0,threshold_gate='insufficient_evidence_no_evaluator')
    (output/'annotation-template.json').write_text(json.dumps(annotation,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result['summary'],ensure_ascii=False))


if __name__=='__main__':main()
