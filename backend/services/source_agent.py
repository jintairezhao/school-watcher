"""Bounded source recognition from observed pages and executed tool feedback.

The onboarding worker owns the observe/execute/repair loop. This module keeps
model decisions replayable across worker restarts and within the shared budget.
"""
from backend.services import tasks


SELECTORS = ('container_selector', 'heading_selector', 'list_selector',
             'title_selector', 'link_selector', 'date_selector', 'content_selector')


def recover_unknown_column_checkpoint(checkpoint):
    """Repair legacy decision counts from execution facts without touching fees.

    Older workers advanced over an unknown paid response. The original snapshot,
    feedback and execution stay authoritative; this repair only pauses that work
    and removes the responses that never became completed decisions.
    """
    import copy
    from flask import has_app_context
    from backend.ai.models import AIExecution
    from backend.database.db import db
    from backend.services.discovery_policy import get_policy
    checkpoint = copy.deepcopy(checkpoint or {})
    if not has_app_context() or checkpoint.get('column_pending_decision'):
        return checkpoint, False
    regions = checkpoint.get('column_regions') or {}
    validation = checkpoint.get('column_validation') or {}
    progress = validation.get('progress') or {}
    ids = list(dict.fromkeys([*regions.get('decision_ids', []), *progress.get('decision_ids', []),
                             *validation.get('trial_decision_ids', [])]))
    unknown = []
    for ident in ids:
        row = db.session.get(AIExecution, ident)
        if (row and row.request_sent and (row.status in ('uncertain', 'sending') or
                row.status in ('failed', 'partial') and not (row.usage or {}).get('known'))):
            unknown.append(row)
    if not unknown:
        return checkpoint, False
    invalid = {row.execution_id for row in unknown}
    original_ids = regions.get('decision_ids', [])
    indexes = [index for index, ident in enumerate(original_ids) if ident in invalid]
    if indexes:
        regions['cursor'] = min(regions.get('cursor', 0), min(indexes))
    regions['decision_ids'] = [ident for ident in original_ids if ident not in invalid]
    regions['completed_materials'] = {key: ident for key, ident in regions.get('completed_materials', {}).items()
                                    if ident not in invalid}
    checkpoint['column_regions'] = regions
    completed = [ident for ident in progress.get('decision_ids', []) if ident not in invalid]
    if progress:
        interval = max(1, int(get_policy()['progress_check_decisions']))
        checked = min(progress.get('checked_decisions', 0), len(completed) // interval * interval)
        misses = min(progress.get('no_change_checks', 0), checked // interval)
        validation['unknown_decision_recovery'] = {'execution_ids': sorted(invalid),
                                                  'original_progress': copy.deepcopy(progress)}
        progress.update(decision_ids=completed, decisions=len(completed), checked_decisions=checked,
                        no_change_checks=misses,
                        stopped=misses >= max(1, int(get_policy()['progress_no_change_checks'])))
        validation['progress'] = progress
    validation['trial_decision_ids'] = [ident for ident in validation.get('trial_decision_ids', []) if ident not in invalid]
    if validation.get('stage') == 'done':
        validation['stage'] = 'repair' if validation.get('material_feedback') else 'ai'
    checkpoint['column_validation'] = validation
    first = min(unknown, key=lambda row: row.created_at)
    checkpoint['column_pending_decision'] = {
        'execution_id': first.execution_id, 'input_digest': first.input_digest,
        'skill_digest': first.skill_digest, 'binding': [first.profile_id, first.config_version],
        'material_hash': regions.get('hash'), 'snapshot': checkpoint.get('onboarding_page'),
        'feedback': validation.get('material_feedback', []), 'blocked': 'result',
        'legacy_execution_ids': [row.execution_id for row in unknown]}
    return checkpoint, True


def pause_unknown_column(checkpoint):
    """Unknown results and unknown bills do not authorize a replacement request."""
    from flask import has_app_context
    from backend.ai.models import AIExecution
    from backend.database.db import db
    plan = checkpoint.get('column_pending_decision') or {}
    if plan.get('blocked') not in ('result', 'usage'):
        return False
    ids = plan.get('legacy_execution_ids') or [plan['execution_id']]
    rows = [db.session.get(AIExecution, ident, populate_existing=True) for ident in ids] if has_app_context() else []
    # Only an actually completed response with settled usage clears this guard.
    # A paid result that remains unknown cannot be retried after a fee adjustment.
    if rows and all(row and row.status == 'succeeded' and (row.usage or {}).get('known') for row in rows):
        checkpoint.pop('column_pending_decision', None)
        if tasks.current_execution():
            tasks.checkpoint(checkpoint)
        return False
    if tasks.current_execution():
        tasks.defer(capability='directory', state='waiting', phase='ai_result',
                    checkpoint=checkpoint, error_code='ai_result_unknown',
                    reason='模型请求结果或用量尚未确认，进度已保存')
    return True


def validation_feedback(column, codes, records=(), *, execution=None, articles=None):
    """Return small observations, never arbitrary model actions or page commands."""
    result = {'name': str(column.get('name', ''))[:120],
              'codes': list(dict.fromkeys(str(code)[:100] for code in codes))[:10]}
    for field in SELECTORS:
        if isinstance(column.get(field), str):
            result[field] = column[field][:500]
    result['sample_records'] = [{key: str(record.get(key) or '')[:limit]
        for key, limit in (('title', 200), ('url', 2000), ('date', 64))}
        for record in list(records)[:3]]
    if execution:
        result['execution'] = {field: execution[field] for field in
            ('container_count', 'heading_count', 'list_item_count')
            if type(execution.get(field)) is int and execution[field] >= 0}
        result['execution']['heading_texts'] = [str(text)[:180] for text in execution.get('heading_texts', [])[:3]]
        result['execution']['selector_errors'] = [str(field)[:40] for field in execution.get('selector_errors', [])[:3]]
        if execution.get('heading_matches'):
            result['execution']['heading_matches'] = [{
                'text': str(row.get('text', ''))[:180], 'locator': str(row.get('locator', ''))[:500],
                'within_declared_container': row.get('within_declared_container') is True,
                'common_group_locator': str(row.get('common_group_locator', ''))[:500]}
                for row in execution['heading_matches'][:3]]
    if articles:
        result['article_trials'] = []
        for sample in articles.get('samples', [])[:3]:
            trial = {key: sample[key] for key in ('url', 'final_url', 'reason', 'error_code', 'matched',
                'title_matched', 'content_selector_count', 'snapshot') if key in sample}
            trial['title'] = str(sample.get('title', ''))[:200]
            result['article_trials'].append(trial)
    return result


def selector_observation(column, html):
    """Explain a failed trial with measured matches from the same original DOM."""
    from bs4 import BeautifulSoup
    from soupsieve import SelectorSyntaxError
    from backend.scraper.discovery.structure import locator
    soup = BeautifulSoup(html, 'lxml')
    result = {'selector_errors': [], 'heading_texts': []}
    matches = {}
    for field, count in (('container_selector', 'container_count'), ('heading_selector', 'heading_count'),
                         ('list_selector', 'list_item_count')):
        if not column.get(field):
            continue
        try:
            nodes = soup.select(column[field])
            matches[field] = nodes
            result[count] = len(nodes)
            if field == 'heading_selector':
                result['heading_texts'] = [node.get_text(' ', strip=True)[:180] for node in nodes[:3]]
        except (SelectorSyntaxError, ValueError, NotImplementedError):
            result['selector_errors'].append(field)
    containers = matches.get('container_selector', [])
    if matches.get('heading_selector'):
        result['heading_matches'] = []
        for heading in matches['heading_selector'][:3]:
            within = any(heading is container or any(parent is container for parent in heading.parents)
                         for container in containers)
            group = None
            if len(containers) == 1:
                container_path = {id(node) for node in [containers[0], *containers[0].parents]}
                group = next((node for node in [heading, *heading.parents] if id(node) in container_path
                              and node.name not in ('[document]', 'html', 'body')), None)
            result['heading_matches'].append({'text': heading.get_text(' ', strip=True)[:180],
                'locator': locator(heading), 'within_declared_container': within,
                'common_group_locator': locator(group) if group is not None else ''})
    return result


def _material_without_heading_locations(material):
    """Identify the precise observation-only upgrade, retaining all old fields."""
    return dict(material, evidence=[{key: value for key, value in proof.items()
                                    if key != 'heading_locations'} for proof in material.get('evidence', [])])


def _material_signature(material):
    from backend.ai.skill_loader import digest
    value = _material_without_heading_locations(material)
    value['evidence'] = [{key: item for key, item in proof.items() if key != 'evidence_id'}
                         for proof in value['evidence']]
    return digest(value)


def _original_column_wait(checkpoint, code, reason):
    if tasks.current_execution():
        tasks.defer(capability='directory', state='waiting', phase='ai_result', checkpoint=checkpoint,
                    error_code=code, reason=reason)
    saved = checkpoint.get('column_regions') or {}
    return {'status': 'partial', 'columns': saved.get('columns', []), 'material_complete': False,
            'error_code': code, 'reason': reason, 'decision_ids': saved.get('decision_ids', [])}


def _confirmed_unsent_column_failure(row):
    from backend.ai.connection_recovery import PAUSED, unsent_pause
    if row.error_code == PAUSED:
        return unsent_pause(row)
    usage = row.usage or {}
    return bool(row.status == 'failed' and row.request_sent is False and usage.get('known') is True
        and type(usage.get('total_tokens')) is int and usage['total_tokens'] == 0
        and all(type(usage.get(key, 0)) is int and usage.get(key, 0) == 0 for key in ('input_tokens', 'output_tokens'))
        and row.reserved_tokens == 0 and not row.output)


def _resume_original_column(checkpoint, school_id, url):
    """Read a durable original result, never compile its ID under a new skill.

    These are candidate selectors only. The onboarding caller still executes its
    scope, title and body checks before installing anything. Column mode has no
    validated partial-item protocol, so a partial response supplies no rules.
    """
    from flask import has_app_context
    from backend.ai.models import AIExecution
    from backend.ai.skill_loader import digest, load_skill, validate_input, validate_output, SkillValidationError
    from backend.database.db import db
    plan = checkpoint.get('column_pending_decision')
    if not plan or not has_app_context():
        return None
    row = db.session.get(AIExecution, plan['execution_id'], populate_existing=True)
    if row is None:
        return None  # Existing guards retain legacy plans lacking a durable row.
    legacy = plan.get('legacy_execution_ids') or []
    if len(legacy) > 1:
        rows = [db.session.get(AIExecution, ident, populate_existing=True) for ident in legacy]
        if not all(item and item.status == 'succeeded' and (item.usage or {}).get('known') for item in rows):
            return None
    usage_known = (row.usage or {}).get('known') is True
    if row.status in ('reserved', 'sending'):
        # Still-active work must be polled, not moved into an indefinite pause.
        # The runtime's existing recovery owns stale sending -> uncertain.
        if tasks.current_execution():
            tasks.defer(capability='directory', delay=10, phase='ai_result', checkpoint=checkpoint,
                        reason='等待原模型请求完成')
        return {'status': 'partial', 'columns': (checkpoint.get('column_regions') or {}).get('columns', []),
                'material_complete': False, 'reason': '等待原模型请求完成', 'decision_ids': []}
    if row.status == 'uncertain' or (not usage_known and row.status != 'succeeded'):
        return _original_column_wait(checkpoint, 'ai_result_unknown', '原模型请求结果或用量尚未确认，进度已保存')
    evidence = plan.get('evidence')
    if not evidence:
        return _original_column_wait(checkpoint, 'original_material_missing', '原模型结果的输入材料尚未恢复，原请求与进度已保留')
    expected_binding = [row.profile_id, row.config_version]
    if (row.purpose != 'directory' or row.skill_id != 'university-source-onboarding' or row.mode != 'column'
            or row.skill_digest != plan.get('skill_digest')
            or list(map(str, plan.get('binding', []))) != list(map(str, expected_binding))
            or row.input_digest != digest({'evidence': evidence, 'repair_feedback': None})
            or str(evidence.get('school_id')) != str(school_id)
            or [item.get('url') for item in evidence.get('candidates', [])] != [url]):
        return _original_column_wait(checkpoint, 'execution_conflict', '原模型结果与保存的输入不匹配，原请求与进度已保留')
    if row.status == 'failed' and row.request_sent is not True:
        if not _confirmed_unsent_column_failure(row):
            return _original_column_wait(checkpoint, 'execution_conflict', '原请求未发送的零费用状态未能确认，进度已保存')
        # A durable, confirmed pre-dispatch failure consumed neither this
        # region nor a paid request. The existing connection/resume gate owns
        # the next attempt; unknown or sent executions never use this path.
        checkpoint.pop('column_pending_decision', None)
        if tasks.current_execution():
            tasks.checkpoint(checkpoint)
        return None
    saved = checkpoint.get('column_regions') or {}
    consumed = (plan.get('consumed') or row.execution_id in saved.get('decision_ids', [])
                and saved.get('cursor', 0) > plan.get('cursor', 0))
    if consumed:
        if (row.usage or {}).get('known'):
            checkpoint.pop('column_pending_decision', None)
            if tasks.current_execution():
                tasks.checkpoint(checkpoint)
        else:
            plan.update(blocked='usage', consumed=True)
            if tasks.current_execution():
                tasks.checkpoint(checkpoint)
        return None
    output = None
    if row.status == 'succeeded':
        try:
            skill = load_skill('university-source-onboarding', 'column')
            validate_input(skill, evidence)
            output = validate_output(skill, row.output, evidence)
        except (SkillValidationError, TypeError, ValueError, KeyError):
            return _original_column_wait(checkpoint, 'execution_conflict', '原模型完整结果未通过输入与输出检查，原请求已保留')
    saved = checkpoint.setdefault('column_regions', {'hash': plan.get('material_hash'), 'cursor': 0,
                                    'columns': [], 'unresolved': [], 'decision_ids': []})
    ident = row.execution_id
    if ident not in saved.setdefault('decision_ids', []):
        saved['decision_ids'].append(ident)
    if output and output['status'] == 'ready':
        saved.setdefault('columns', []).extend(output['columns'])
    elif not output or output['status'] == 'unsupported':
        index = plan.get('cursor', saved.get('cursor', 0))
        if index not in saved.setdefault('unresolved', []):
            saved['unresolved'].append(index)
    if output:
        saved.setdefault('completed_materials', {})[_material_signature(evidence)] = ident
    saved['cursor'] = max(saved.get('cursor', 0), plan.get('cursor', saved.get('cursor', 0)) + 1)
    saved['original_result_recovered'] = True
    if (row.usage or {}).get('known'):
        checkpoint.pop('column_pending_decision', None)
    else:
        plan.update(blocked='usage', consumed=True)
    if tasks.current_execution():
        tasks.checkpoint(checkpoint)
    columns = list({(item['name'], item['list_selector']): item for item in saved.get('columns', [])}.values())
    return {'status': 'ready' if columns else 'partial', 'columns': columns,
            'material_complete': False,
            'decision_ids': saved['decision_ids'], 'unresolved_regions': len(saved.get('unresolved', [])),
            'reason': '已恢复原模型结果，继续验证规则及未完成区域'}


def recognize_column(school_id, url, html, *, feedback=None):
    """Ask for executable selectors, retaining the failed trial on a repair turn."""
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.ai.skill_loader import canonical, digest, load_skill
    from backend.services.discovery_policy import get_policy
    handle = tasks.current_execution() or {}
    checkpoint, recovered = recover_unknown_column_checkpoint(handle.get('checkpoint') or {})
    if recovered and handle:
        tasks.checkpoint(checkpoint)
    original = _resume_original_column(checkpoint, school_id, url)
    if original is not None:
        return original
    if pause_unknown_column(checkpoint):
        return {'status': 'partial', 'columns': (checkpoint.get('column_regions') or {}).get('columns', []),
                'material_complete': False, 'error_code': 'ai_result_unknown', 'decision_ids': []}
    try:
        binding = get_model_binding('directory')
    except AIConfigError:
        return {'status': 'unsupported', 'columns': [], 'reason': '该页面暂未识别，可启用 AI 辅助解析'}
    from backend.scraper.discovery.column_regions import materials
    maximum = get_policy()['evidence_max_bytes']
    if feedback:
        groups, group = [], []
        for row in feedback:
            observation = validation_feedback(row, row.get('codes', []), row.get('sample_records', [])[:1],
                                              execution=row.get('execution'), articles={'samples': row.get('article_trials', [])})
            if group and len(canonical(group + [observation]).encode()) > maximum // 3:
                groups.append(group)
                group = []
            group.append(observation)
        if group:
            groups.append(group)
        regions = []
        for observations in groups:
            overhead = len(canonical({'tool_feedback': observations}).encode()) + 64
            preferred = [row[field] for row in observations for field in
                         ('container_selector', 'list_selector', 'heading_selector') if row.get(field)]
            trials = [trial for observation in observations for trial in observation.get('article_trials', [])
                      if trial.get('snapshot')]
            available = maximum - overhead
            listing = materials(school_id, url, html, max_bytes=available // 2 if trials else available,
                                preferred_selectors=preferred)
            if trials and listing:
                from backend.services.source_governance import read_snapshot
                for index, trial in enumerate(trials):
                    try:
                        document = read_snapshot(trial['snapshot'])
                    except (ValueError, OSError):
                        # Missing local evidence is explicit; never fabricate it
                        # or make an unbudgeted replacement website request here.
                        trial['evidence_state'] = 'unavailable'
                        continue
                    extra = {'role': 'article_sample', 'listed_title': trial['title'], 'list_url': url}
                    body_limit = available - len(canonical(listing[0]).encode()) - len(canonical(extra).encode()) - 128
                    for body in materials(school_id, trial.get('final_url') or trial['url'], document,
                                          max_bytes=body_limit,
                                          preferred_selectors=('article', '[itemprop="articleBody"]',
                                              '.Article_content', '.v_news_content', '.wp_articlecontent', '.TRS_Editor')):
                        combined = dict(listing[0], evidence=list(listing[0]['evidence']), tool_feedback=observations)
                        for proof in body['evidence']:
                            augmented = dict(proof, evidence_id=f'article-{index}-' + proof['evidence_id'], **extra)
                            proposed = dict(combined, evidence=combined['evidence'] + [augmented])
                            if len(canonical(proposed).encode()) > maximum:
                                if len(combined['evidence']) == len(listing[0]['evidence']):
                                    raise ValueError('Article evidence envelope exceeds the configured input limit')
                                regions.append(combined)
                                combined = dict(listing[0], evidence=list(listing[0]['evidence']), tool_feedback=observations)
                            combined['evidence'].append(augmented)
                        regions.append(combined)
            regions.extend(dict(region, tool_feedback=observations) for region in listing)
    else:
        regions = materials(school_id, url, html)
    if not regions:
        return {'status': 'unsupported', 'columns': [], 'reason': '尚未定位到可独立读取的发布区域，入口已保留'}
    material_hash = digest(regions)
    pending = checkpoint.get('column_pending_decision')
    if pending and (pending.get('material_hash') != material_hash or
                    list(map(str, pending.get('binding', []))) != [str(binding['id']), str(binding['version'])]):
        if handle:
            tasks.defer(capability='directory', state='waiting', phase='ai_result', checkpoint=checkpoint,
                        error_code='execution_conflict', reason='原模型请求仍待确认，保留原材料与配置')
        return {'status': 'partial', 'columns': [], 'material_complete': False,
                'error_code': 'execution_conflict', 'decision_ids': []}
    saved = checkpoint.get('column_regions', {})
    if saved.get('hash') != material_hash:
        observation_hash = digest([_material_without_heading_locations(region) for region in regions])
        if saved.get('hash') == observation_hash or saved.get('observation_hash') == observation_hash:
            # Adding measured DOM locations cannot invalidate completed work or
            # request its paid decisions again. HTML, feedback and every other
            # original field must be identical for this exact cursor migration.
            saved.update(hash=material_hash, observation_hash=observation_hash)
        else:
            retained = saved if saved.get('original_result_recovered') and not feedback else {}
            saved = {'hash': material_hash, 'observation_hash': observation_hash, 'cursor': 0,
                'columns': retained.get('columns', []), 'unresolved': [],
                'decision_ids': retained.get('decision_ids', []),
                'completed_materials': retained.get('completed_materials', {})}
    skill = load_skill('university-source-onboarding', 'column')
    for index in range(saved['cursor'], len(regions)):
        evidence = regions[index]
        if _material_signature(evidence) in saved.get('completed_materials', {}):
            # A changed partition is complete only at a previously completed
            # exact evidence boundary, never because one old item overlapped.
            saved['cursor'] = index + 1
            checkpoint['column_regions'] = saved
            if handle:
                tasks.checkpoint(checkpoint)
            continue
        execution_id = 'column-region:' + digest([skill.resource_digest,
            evidence, binding['id'], binding['version']])
        from backend.ai.connection_recovery import resumed_execution_id
        execution_id = resumed_execution_id(execution_id, checkpoint=checkpoint)
        pending = checkpoint.get('column_pending_decision')
        if pending and pending['execution_id'] != execution_id:
            if handle:
                tasks.defer(capability='directory', state='waiting', phase='ai_result', checkpoint=checkpoint,
                            error_code='execution_conflict', reason='原模型请求仍待确认，保留原材料与配置')
            return {'status': 'partial', 'columns': saved['columns'], 'material_complete': False,
                    'error_code': 'execution_conflict', 'decision_ids': saved.get('decision_ids', [])}
        checkpoint['column_regions'] = saved
        checkpoint['column_pending_decision'] = {'execution_id': execution_id, 'evidence': evidence,
            'binding': [binding['id'], binding['version']], 'skill_digest': skill.resource_digest,
            'material_hash': material_hash, 'cursor': index}
        if handle:
            tasks.checkpoint(checkpoint)
        try:
            result = run_skill('university-source-onboarding', 'column', evidence, 'directory', execution_id, binding=binding)
        except AIConfigError as exc:
            if exc.code in ('concurrency_limit', 'budget_exhausted', 'provider_connection_paused'):
                # These are confirmed pre-dispatch rejections, never a paid plan.
                checkpoint.pop('column_pending_decision', None)
                if handle:
                    tasks.checkpoint(checkpoint)
            if exc.code == 'provider_connection_paused' and handle:
                from backend.ai.connection_recovery import defer_connection
                defer_connection(binding, checkpoint=checkpoint)
            if exc.code == 'concurrency_limit' and handle:
                tasks.defer(capability='directory', delay=10, reason='等待 AI 空闲', checkpoint=checkpoint)
            columns = list({(c['name'], c['list_selector']): c for c in saved['columns']}.values())
            return {'status': 'ready' if columns else 'unsupported', 'columns': columns, 'reason':
                    '本轮 AI 用量已达上限，未完成入口已保留' if exc.code == 'budget_exhausted' else 'AI 辅助暂不可用，其他栏目继续接入',
                    'unresolved_regions': len(saved['unresolved']) + len(regions) - index,
                    'material_complete': False, 'remaining_regions': len(regions) - index,
                    'decision_ids': saved.get('decision_ids', []),
                    'error_code': exc.code}
        actual_id = (result.get('provenance') or {}).get('execution_id')
        if actual_id:
            execution_id = actual_id
            checkpoint['column_pending_decision']['execution_id'] = actual_id
        if result.get('error_code') == 'provider_connection_paused':
            # A cached preflight failure is the same connection handoff as the
            # exception path, not a completed model decision or an empty column.
            if handle:
                from backend.ai.connection_recovery import defer_connection
                defer_connection(binding, checkpoint=checkpoint)
            return {'status': 'partial', 'columns': saved['columns'], 'material_complete': False,
                    'error_code': 'provider_connection_paused', 'decision_ids': saved.get('decision_ids', [])}
        if result['status'] == 'pending':
            if handle:
                tasks.defer(capability='directory', delay=10, reason='等待已提交的栏目识别', checkpoint=checkpoint)
            return {'status': 'partial', 'columns': saved['columns'], 'material_complete': False,
                    'decision_ids': saved.get('decision_ids', []), 'reason': '等待已提交的栏目识别'}
        unknown_result = (result['status'] == 'uncertain' or result['status'] != 'succeeded'
                          and result.get('usage', {}).get('known') is False)
        if unknown_result:
            checkpoint['column_pending_decision']['blocked'] = 'result'
            if handle:
                tasks.checkpoint(checkpoint)
            pause_unknown_column(checkpoint)
            return {'status': 'partial', 'columns': saved['columns'], 'material_complete': False,
                    'error_code': 'ai_result_unknown', 'decision_ids': saved.get('decision_ids', [])}
        usage_unknown = result['status'] == 'succeeded' and result.get('usage', {}).get('known') is False
        if usage_unknown:
            checkpoint['column_pending_decision']['blocked'] = 'usage'
        else:
            checkpoint.pop('column_pending_decision', None)
        if result['status'] != 'pending' and execution_id not in saved.setdefault('decision_ids', []):
            saved['decision_ids'].append(execution_id)
        if result['status'] == 'succeeded' and result['output']['status'] == 'ready':
            saved['columns'].extend(result['output']['columns'])
        elif result['status'] != 'succeeded' or result['output']['status'] == 'unsupported':
            saved['unresolved'].append(index)
        if result['status'] == 'succeeded':
            saved.setdefault('completed_materials', {})[_material_signature(evidence)] = execution_id
        saved['cursor'] = index + 1
        checkpoint['column_regions'] = saved
        if handle:
            tasks.checkpoint(checkpoint)
            if index + 1 < len(regions):
                columns = list({(c['name'], c['list_selector']): c for c in saved['columns']}.values())
                return {'status': 'ready' if columns else 'partial', 'columns': columns,
                        'material_complete': False, 'remaining_regions': len(regions) - index - 1,
                        'decision_ids': saved.get('decision_ids', []),
                        'unresolved_regions': len(saved['unresolved']), 'reason': '继续读取其余发布区域'}
        if usage_unknown:
            # The existing decision is useful; its unknown bill cannot justify
            # another paid action, even in a synchronous caller without a task.
            break
    columns = list({(c['name'], c['list_selector']): c for c in saved['columns']}.values())
    complete = saved['cursor'] >= len(regions)
    return {'status': 'ready' if columns else 'partial' if not complete else 'unsupported' if saved['unresolved'] else 'not_column', 'columns': columns,
            'reason': '部分发布区域仍待识别' if saved['unresolved'] else '',
            'decision_ids': saved.get('decision_ids', []),
            'material_complete': complete, 'remaining_regions': max(0, len(regions) - saved['cursor']),
            'unresolved_regions': len(saved['unresolved'])}
