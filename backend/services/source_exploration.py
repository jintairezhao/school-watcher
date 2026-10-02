"""Bounded interpreter for the bundled onboarding skill; never executes model code.

Review events are a durable journal: inputs are saved before dispatch, outputs before
operations, and each acquired page before the next operation. A resumed worker uses
the same paid execution ID. The existing acquisition and validation gates own I/O.
"""
import json
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from flask import current_app
from backend.database.db import db
from backend.database.source_governance_models import SourceProposal, SourceReviewEvent
from backend.services.source_inventory import canonical_url

STEP_ACTION = 'exploration_step_v2'
MAX_CALLS = 4
MAX_READS = 8
MAX_MATERIAL_ROUNDS = 3
PROPOSAL_TOKENS = 240000
SCHOOL_TOKENS = 1200000
REFERENCES = ('identity', 'page-types', 'extraction-patterns')


def events(proposal_id, action=STEP_ACTION):
    return SourceReviewEvent.query.filter_by(proposal_id=proposal_id, action=action).order_by(SourceReviewEvent.id).all()


def charged_tokens(detail):
    """Settle a reservation only against provider-reported, known usage."""
    usage = (detail.get('result') or {}).get('usage') or {}
    total = usage.get('total_tokens')
    if usage.get('known') is True and type(total) is int and total >= 0:
        return total
    return detail.get('reservation', 0)


def save_event(proposal_id, action, detail):
    from backend.services.source_governance import _json
    event = SourceReviewEvent(proposal_id=proposal_id, action=action, detail_json=_json(detail))
    db.session.add(event); db.session.commit()
    return event


def references(bundle):
    result = []
    for name in ('list', 'independent', 'pagination'):
        if bundle.get(name):
            result.append((name, bundle[name]))
    for name in ('articles', 'identity_snapshots', 'exploration_pages'):
        result.extend((f'{name}-{i}', ref) for i, ref in enumerate(bundle.get(name, [])))
    # Current captures take precedence over old publisher material for the same
    # URL. Older releases could retain a homepage under a changed list address.
    known = {canonical_url(ref['url']) for _, ref in result}
    for ident, ref in (bundle.get('publisher_material') or {}).items():
        key = canonical_url(ref['url'])
        if key not in known:
            result.append(('prior-' + ident, ref)); known.add(key)
    return result


def material(proposal):
    from backend.services.source_governance import read_snapshot
    from backend.database.models import School
    from backend.scraper.http_client import same_school_url, validate_public_url
    config, bundle = json.loads(proposal.candidate_json), json.loads(proposal.evidence_json)
    root = db.session.get(School, proposal.school_id).url
    observed = {canonical_url(root), canonical_url(config['list_url'])}
    evidence, links = [], []
    rendered = {}
    for ident, ref in references(bundle):
        html = read_snapshot(ref)
        soup = BeautifulSoup(html, 'lxml')
        for node in soup.select('script,style,iframe,noscript,svg'):
            node.decompose()
        for node in soup.find_all(True):
            # Presentation and event code are neither evidence nor executable
            # operations. Preserve structural attributes for AI extraction.
            for attr in list(node.attrs):
                if attr == 'style' or attr.startswith('on') or (attr == 'src' and str(node[attr]).startswith('data:')):
                    del node[attr]
        observed.add(canonical_url(ref['url']))
        for anchor in soup.select('a[href]'):
            url = canonical_url(urljoin(ref['url'], anchor['href']))
            try:
                validate_public_url(url, resolve=False)
            except ValueError:
                continue
            # Off-site links are visible as evidence, never silently treated as a school unit.
            if url not in observed:
                links.append({'url': url, 'text': anchor.get_text(' ', strip=True)[:160],
                              'from': ref['url'], 'school_domain': same_school_url(url, root)})
            observed.add(url)
        if ref['hash'] in rendered:
            evidence.append({'evidence_id': ident, 'url': ref['url'], 'role': ref.get('role'),
                             'same_content_as': rendered[ref['hash']]})
            continue
        import re
        document = re.sub(r'>\s+<', '><', str(soup))
        snippet = document
        rendered[ref['hash']] = ident
        evidence.append({'evidence_id': ident, 'url': ref['url'], 'role': ref.get('role'),
                         'outcome': ref.get('outcome'), 'html': snippet, 'truncated': len(document) > len(snippet)})
    for name in bundle.get('skill_references', []):
        if name in REFERENCES:
            from backend.ai.skill_loader import ROOT
            evidence.append({'evidence_id': 'reference-' + name, 'text':
                (ROOT / 'university-source-onboarding' / 'references' / (name + '.md')).read_text(encoding='utf-8')})
    return {'school_id': proposal.school_id, 'candidates': [{'candidate_id': f'source-{proposal.id}',
            'config': config, 'validation': json.loads(proposal.validation_json)}],
        'entities': [{'id': p['unit_key'], 'name': p['unit_name']} for p in bundle.get('identity_paths', [])],
        'evidence': evidence, 'observed_urls': sorted(observed), 'links': links,
        'acquisition_results': bundle.get('samples', {}),
        'operation_results': bundle.get('operation_results', []),
        'limits': {'calls': MAX_CALLS, 'material_rounds': MAX_MATERIAL_ROUNDS, 'pages': MAX_READS,
                   'token_budget': PROPOSAL_TOKENS}, 'available_references': list(REFERENCES)}


def set_workflow(proposal, status, **detail):
    from backend.services.source_governance import _json
    validation = json.loads(proposal.validation_json)
    previous = validation.get('workflow', {})
    if detail.get('ai_status') == 'succeeded' and detail.get('reason'):
        detail['ai_reason'] = detail['reason']
    validation['workflow'] = dict(previous, status=status, **detail)
    proposal.validation_json = _json(validation)
    db.session.commit()


def acquire(proposal, action, observed):
    from backend.services import source_governance as g, tasks
    from backend.scraper.acquisition import FetchFailure, FetchDeferred
    from backend.scraper.http_client import validate_public_url, same_school_url
    from backend.database.models import School
    bundle = json.loads(proposal.evidence_json)
    kind = action.get('type')
    if kind == 'read_reference':
        name = action.get('reference')
        if name not in REFERENCES:
            return {'status': 'invalid_reference'}
        names = bundle.setdefault('skill_references', [])
        if name not in names:
            names.append(name)
        result = {'status': 'obtained', 'reference': name}
    elif kind == 'read_page':
        url, purpose = canonical_url(action.get('url', '')), action.get('purpose')
        if url not in observed or purpose not in ('directory', 'list', 'article'):
            return {'status': 'unsupported_operation', 'reason': '网址未在官网材料中出现，或读取用途无效'}
        validate_public_url(url, resolve=False)
        root = db.session.get(School, proposal.school_id).url
        if not same_school_url(url, root):
            return {'status': 'external_identity_required', 'url': url}
        cached = next((ref for _, ref in references(bundle) if canonical_url(ref['url']) == url), None)
        if cached:
            return {'status': 'cached', 'url': url, 'hash': cached['hash']}
        if len(events(proposal.id, 'exploration_read')) >= MAX_READS:
            return {'status': 'page_budget_exhausted'}
        # A failed URL is not reread indefinitely within the same exploration.
        previous = next((x for x in bundle.get('operation_results', []) if x.get('url') == url), None)
        if previous:
            return previous
        tasks.assert_owned()
        db.session.commit()
        try:
            html = g._fetch(url, purpose)
            ref = g._snapshot(html, getattr(html, 'final_url', url), role=purpose)
            bundle.setdefault('exploration_pages', []).append(ref)
            result = {'status': 'obtained', 'url': url, 'hash': ref['hash'], 'purpose': purpose}
        except FetchDeferred:
            raise
        except FetchFailure as exc:
            result = {'status': 'login_required' if exc.error_code == 'source_login_required' else
                      'access_limited' if g.gated_sample(exc) else 'fetch_failed',
                      'url': url, 'error_code': exc.error_code, 'reason': str(exc)[:400]}
            # An unfamiliar but readable document is still material for AI. It is
            # never a successful validation sample until the normal gate accepts it.
            if exc.result.html and exc.outcome not in ('denied', 'needs_manual', 'network_error', 'unavailable'):
                bundle.setdefault('exploration_pages', []).append(g._snapshot(exc.result.html,
                    exc.result.final_url or url, role=purpose, outcome=exc.outcome))
        db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='exploration_read', detail_json=g._json(result)))
    else:
        return {'status': 'unsupported_operation'}
    bundle.setdefault('operation_results', []).append(result)
    proposal.evidence_json, proposal.evidence_hash = g._json(bundle), g._hash(bundle)
    db.session.commit()
    return result


def inferred_actions(proposal, evidence):
    """Compatibility with v1 review responses: acquire actual missing materials."""
    from backend.services import source_governance as g
    from backend.scraper.engine import _next_page_url
    bundle, config = json.loads(proposal.evidence_json), json.loads(proposal.candidate_json)
    actions = []
    if config.get('list_selector'):
        _, _, records, _ = g._extract(config, bundle['list'])
        present = {canonical_url(ref['url']) for _, ref in references(bundle)}
        actions += [{'type': 'read_page', 'url': r['url'], 'purpose': 'article', 'reason': '补充通知正文'}
                    for r in records[:3] if r['url'] not in present]
    next_url = _next_page_url(g.read_snapshot(bundle['list']), bundle['list']['url'], 1)
    if next_url and next_url in evidence['observed_urls']:
        actions.append({'type': 'read_page', 'url': next_url, 'purpose': 'list', 'reason': '补充分页'})
    # Identity may require the root/department navigation, never a guessed address.
    actions += [{'type': 'read_page', 'url': url, 'purpose': 'directory', 'reason': '核对官方机构入口'}
                for url in [bundle.get('root_url')] if url and url != bundle['list']['url']]
    return actions[:3]


def run(proposal_id, *, inventory=None):
    from backend.services import source_governance as g, tasks
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.services.directory_work import run_material
    from backend.ai.skill_loader import load_skill, canonical
    proposal = db.session.get(SourceProposal, proposal_id)
    handle = tasks.current_execution()
    saved_only = bool(handle and handle.get('payload', {}).get('saved_results_only'))
    if not proposal or proposal.state in ('activated', 'rejected', 'superseded'):
        return {'status': 'skipped', 'changed': False}
    if not json.loads(proposal.evidence_json).get('list'):
        return {'status': 'missing_evidence', 'changed': False}
    # Rechecks made by older versions could replace the primary evidence bundle.
    # Restore obtained pages from the journal instead of paying to reread them.
    bundle = json.loads(proposal.evidence_json)
    hashes = {ref['hash'] for _, ref in references(bundle)}
    restored = False
    for record in events(proposal.id, 'exploration_read'):
        entry = json.loads(record.detail_json)
        if entry.get('status') == 'obtained' and entry.get('hash') not in hashes:
            ref = {'hash': entry['hash'], 'url': entry['url'], 'role': entry.get('purpose', 'directory'),
                   'outcome': 'usable', 'captured_at': record.created_at.isoformat()}
            try:
                g.read_snapshot(ref)
            except (ValueError, OSError):
                continue
            bundle.setdefault('exploration_pages', []).append(ref)
            bundle.setdefault('operation_results', []).append(entry)
            hashes.add(ref['hash']); restored = True
    if restored:
        proposal.evidence_json, proposal.evidence_hash = g._json(bundle), g._hash(bundle)
        db.session.commit()
    try:
        binding = get_model_binding('directory')
    except AIConfigError as exc:
        if not saved_only:
            set_workflow(proposal, 'recognition_incomplete', ai_status='not_configured', reason=str(exc))
            return {'status': 'not_configured', 'changed': False}
        binding = None
    changed = False
    legacy_attempts = sum(json.loads(e.detail_json).get('status') in (None, 'pending', 'uncertain', 'failed')
                          for e in events(proposal.id, 'skill_attempt'))
    for _ in range(MAX_CALLS + 1):
        tasks.assert_owned()
        # Serialize allocation of a paid step, including standalone review callers.
        db.session.execute(db.update(SourceProposal).where(SourceProposal.id == proposal.id)
                           .values(updated_at=datetime.utcnow()))
        db.session.refresh(proposal)
        journal = events(proposal.id)
        pending = next((e for e in journal if not json.loads(e.detail_json).get('applied')), None)
        preflights = [json.loads(e.detail_json).get('preflight') for e in journal]
        failed_plans = [g._json(p['proposed_config']) for p in preflights if p and p.get('proposed_config')]
        repeated_plan = any(failed_plans.count(plan) >= 2 for plan in set(failed_plans))
        if not pending and (repeated_plan or len(journal) >= MAX_CALLS or sum(
                (json.loads(e.detail_json).get('validation') or {}).get('validator_version') == g.VERSION for e in journal) >= 2):
            db.session.commit(); break
        if pending:
            detail = json.loads(pending.detail_json)
            if detail.get('status') in ('pending', 'uncertain', 'failed', 'partial') and not detail.get('manifest_started'):
                from backend.ai.models import AIExecution
                original = db.session.get(AIExecution, detail.get('execution_id')) if detail.get('execution_id') else None
                if original and original.status in ('reserved', 'sending'):
                    db.session.commit()
                    if tasks.current_execution():
                        tasks.defer(capability='directory', phase='source_material', delay=5,
                                    reason='等待旧调用心跳恢复核对，保留原调用账目')
                    return {'status': 'pending', 'changed': changed}
                # Old inputs were silently truncated. Keep their journal intact
                # and allocate a new manifest step from the full saved snapshots.
                detail.update(applied=True, recovered_by_manifest=True)
                pending.detail_json = g._json(detail); db.session.commit()
                evidence = material(proposal)
                detail = {'execution_id': f'manifest-journal:{proposal.id}:{pending.id}',
                    'attempt': len(journal) + 1, 'revision': proposal.revision, 'input': evidence,
                    'reservation': len(canonical(evidence).encode()), 'binding_version': binding['version'],
                    'applied': False, 'manifest_started': True, 'previous_event_id': pending.id,
                    'legacy_attempts': max(legacy_attempts, detail.get('attempt', 1))}
                pending = save_event(proposal.id, STEP_ACTION, detail)
            if detail.get('revision') != proposal.revision:
                detail['applied'] = True; detail['discarded'] = 'revision_changed'
                pending.detail_json = g._json(detail); db.session.commit(); continue
            evidence = detail.get('input') or material(proposal)
            event = pending
        else:
            from backend.database.models import School
            db.session.execute(db.update(School).where(School.id == proposal.school_id).values(name=School.name))
            evidence = material(proposal)
            reservation = len(canonical(evidence).encode()) + len(load_skill('university-source-onboarding', 'extraction').prompt.encode()) + int(binding.get('max_output_tokens', 8000))
            school_events = db.session.query(SourceReviewEvent).join(SourceProposal).filter(
                SourceProposal.school_id == proposal.school_id, SourceReviewEvent.action == STEP_ACTION).all()
            attempt = len(journal) + 1
            detail = {'execution_id': f'explore-v2:{proposal.id}:{attempt}:{proposal.evidence_hash[:16]}',
                'attempt': attempt, 'revision': proposal.revision, 'input': evidence, 'reservation': reservation,
                'binding_version': binding['version'], 'applied': False}
            event = save_event(proposal.id, STEP_ACTION, detail)
        db.session.commit()
        from backend.services.directory_work import extraction_suggestion
        saved_suggestion, _ = extraction_suggestion(
            ((detail.get('result') or {}).get('output') or {}).get('proposals', []), evidence)
        if saved_suggestion and saved_suggestion.get('decision') == 'propose':
            result = {'status': 'partial', 'output': {'proposals': [saved_suggestion]}, 'material_complete': False}
        elif detail.get('status') != 'succeeded':
            if saved_only:
                set_workflow(proposal, 'recognition_incomplete', ai_status='needs_recovery',
                             reason='已保存结果尚不足以完成接入，保留缺口与原调用次数')
                return {'status': 'needs_recovery', 'changed': changed}
            set_workflow(proposal, 'checking', ai_status='running', reason='AI 正在判断已有官网材料')
            try:
                from backend.database.models import BackgroundTask
                parent = BackgroundTask.query.filter_by(identity=f'discover:{proposal.school_id}').first()
                generation = parent.generation if parent else 1
                prior_attempts = detail.get('legacy_attempts', legacy_attempts)
                if detail.get('status') in ('failed', 'uncertain') and not detail.get('manifest_started'):
                    prior_attempts = max(prior_attempts, detail.get('attempt', 1))
                result = run_material(proposal.school_id, generation, f'source:{proposal.id}',
                    'extraction', evidence, binding, existing_attempts=prior_attempts)
                detail['manifest_started'] = True
            except AIConfigError as exc:
                if exc.code == 'concurrency_limit' and tasks.current_execution():
                    set_workflow(proposal, 'checking', ai_status='waiting', reason='AI 正在处理其他页面，材料已保存并排队')
                    tasks.defer(capability='directory', phase='source_material', delay=5,
                                reason='等待 AI 空闲，未产生新的付费调用')
                set_workflow(proposal, 'recognition_incomplete', ai_status=exc.code, reason=str(exc))
                return {'status': exc.code, 'changed': changed}
            detail.update(status=result.get('status'), result=result)
            event.detail_json = g._json(detail); db.session.commit()
        else:
            result = detail['result']
        if result.get('status') not in ('succeeded', 'partial'):
            set_workflow(proposal, 'recognition_incomplete', ai_status=result.get('status'),
                         reason='AI 调用未完成：' + (result.get('error_detail') or result.get('error_code') or '结果未知；保留进度，不自动重复计费'))
            if result.get('status') == 'pending' and tasks.current_execution():
                set_workflow(proposal, 'checking', ai_status='waiting', reason='成功结果已保存，等待补齐剩余项')
                tasks.defer(capability='directory', phase='source_material', delay=result.get('next_delay') or 1,
                            reason='等待处理剩余材料或有限补试')
            return {'status': result.get('status'), 'changed': changed}
        suggestion = (result.get('output') or {}).get('proposals', [{}])[0]
        if suggestion.get('candidate_id') != f'source-{proposal.id}':
            set_workflow(proposal, 'recognition_incomplete', ai_status='invalid_reference')
            return {'status': 'invalid_reference', 'changed': changed}
        reason = suggestion.get('reason', '')
        missing = suggestion.get('needed_evidence', [])
        observed = set(evidence['observed_urls'])
        decision = suggestion.get('decision')
        if decision == 'not_column':
            proposal.state = 'not_applicable'
            set_workflow(proposal, 'not_a_column', ai_status='succeeded', reason=reason)
            detail['applied'] = True
            event.detail_json = g._json(detail); db.session.commit()
            return {'status': 'not_a_column', 'changed': False}
        if decision == 'propose':
            new_config = g._candidate(suggestion['config'])
            if canonical_url(new_config['list_url']) not in observed:
                return {'status': 'invalid_reference', 'changed': changed}
            old_bundle = json.loads(proposal.evidence_json)
            list_ref = next((r for _, r in references(old_bundle)
                             if canonical_url(r['url']) == canonical_url(new_config['list_url'])
                             and r.get('role') in ('list', 'independent_list') and r.get('outcome') == 'usable'), None)
            if list_ref and not BeautifulSoup(g.read_snapshot(list_ref), 'lxml').select(new_config['list_selector']):
                # A selector typo in material we already have is immediate
                # feedback, not a reason to spend a browser timeout or body reads.
                feedback = {'status': 'validation_failed', 'stage': 'selector_check', 'url': new_config['list_url'],
                            'selector': new_config['list_selector'], 'matched': 0, 'proposed_config': new_config}
                old_bundle.setdefault('operation_results', []).append(feedback)
                proposal.evidence_json, proposal.evidence_hash = g._json(old_bundle), g._hash(old_bundle)
                detail.update(applied=True, preflight=feedback)
                event.detail_json = g._json(detail); db.session.commit()
                continue
            if not detail.get('config_applied'):
                detail['previous_config'] = json.loads(proposal.candidate_json)
                proposal.candidate_json = g._json(new_config); proposal.revision += 1
                proposal.validated_hash = None; proposal.state = 'proposed'
                detail['config_applied'] = True; detail['revision'] = proposal.revision
                event.detail_json = g._json(detail); db.session.commit()
            old_bundle = json.loads(proposal.evidence_json)
            def cached_fetch(url, purpose):
                from backend.scraper.acquisition import FetchedHTML, FetchResult
                role = 'independent_list' if purpose == 'independent_list' else 'body' if purpose == 'body' else purpose
                ref = next((r for _, r in references(old_bundle) if canonical_url(r['url']) == canonical_url(url)
                    and r.get('outcome', 'usable') in ('usable', 'needs_adapter')
                    and (r.get('role') == role or role == 'body' and r.get('role') == 'article')), None)
                if ref:
                    from backend.scraper.acquisition import FetchRequest, classify_result
                    raw = FetchResult(ref['url'], html=g.read_snapshot(ref), outcome='usable', status=200)
                    checked = classify_result(FetchRequest(url=ref['url'], purpose='article',
                        readiness_selector=new_config.get('content_selector', '')), raw) if role == 'body' else raw
                    if checked.ok:
                        return FetchedHTML(checked)
                return g._fetch(url, purpose, config=new_config)
            set_workflow(proposal, 'checking', ai_status='succeeded', reason=reason, needed_evidence=missing)
            captured = g.capture_source_evidence(proposal.school_id, new_config, department_id=proposal.department_id,
                                                inventory=inventory, fetcher=cached_fetch,
                                                scope_evidence=suggestion.get('scope_evidence'),
                                                publisher_material={ident: ref for ident, ref in references(old_bundle)})
            captured.bundle['exploration_pages'] = old_bundle.get('exploration_pages', []) + captured.bundle.get('exploration_pages', [])
            for key in ('skill_references', 'operation_results'):
                captured.bundle[key] = old_bundle.get(key, [])
            # Changing from a homepage widget to its actual list must retain the
            # observed route, even when AI correctly declares a school publisher
            # and therefore has no department-directory claim to cite.
            if suggestion.get('publisher_evidence'):
                captured.bundle['publisher_evidence'] = suggestion['publisher_evidence']
            validation = g.validate_proposal(proposal.id, independent_evidence=captured)
            set_workflow(proposal, 'checking' if validation['passed'] else 'recognition_incomplete',
                         ai_status='succeeded', reason=reason, needed_evidence=missing)
            changed = True
            detail['applied'] = True; detail['validation'] = validation
            event.detail_json = g._json(detail); db.session.commit()
            if validation['passed']:
                return {'status': 'validated', 'changed': True}
            if saved_only:
                return {'status': 'needs_recovery', 'changed': True}
            continue
        actions = suggestion.get('actions') or inferred_actions(proposal, evidence)
        if decision == 'choose' and suggestion.get('question'):
            from backend.services.source_workflow import supported_question
            question = supported_question(proposal, suggestion['question'], evidence)
            if question:
                set_workflow(proposal, 'user_choice', ai_status='succeeded', reason=reason, question=question, needed_evidence=missing)
                detail['applied'] = True; event.detail_json = g._json(detail); db.session.commit()
                return {'status': 'user_choice', 'changed': changed}
        rounds = sum(bool(json.loads(e.detail_json).get('operations')) for e in journal)
        results = []
        if actions and rounds < MAX_MATERIAL_ROUNDS:
            set_workflow(proposal, 'checking', ai_status='succeeded', reason=reason, needed_evidence=missing)
            for action in actions[:3]:
                results.append(acquire(proposal, action, observed))
        detail['operations'] = results; detail['applied'] = True
        event.detail_json = g._json(detail); db.session.commit()
        if not any(r.get('status') in ('obtained', 'fetch_failed', 'login_required', 'access_limited') for r in results):
            set_workflow(proposal, 'recognition_incomplete', ai_status='succeeded', reason=reason,
                         needed_evidence=missing, operations=results)
            return {'status': 'review_required', 'changed': changed}
        if tasks.current_execution():
            tasks.defer(capability='directory', phase='source_material', delay=0,
                        checkpoint=tasks.current_execution().get('checkpoint'), reason='补充材料已保存，等待下一轮判断')
    set_workflow(proposal, 'recognition_incomplete', ai_status='limit_reached',
                 reason='本轮自动识别未完成，已保存取得的材料和判断，等待进一步适配')
    return {'status': 'review_required', 'changed': changed}
