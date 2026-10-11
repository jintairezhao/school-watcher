"""Bounded source recognition from observed pages and executed tool feedback.

The onboarding worker owns the observe/execute/repair loop. This module keeps
model decisions replayable across worker restarts and within the shared budget.
"""
from backend.services import tasks


SELECTORS = ('container_selector', 'heading_selector', 'list_selector',
             'title_selector', 'link_selector', 'date_selector', 'content_selector')


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
    soup = BeautifulSoup(html, 'lxml')
    result = {'selector_errors': [], 'heading_texts': []}
    for field, count in (('container_selector', 'container_count'), ('heading_selector', 'heading_count'),
                         ('list_selector', 'list_item_count')):
        if not column.get(field):
            continue
        try:
            nodes = soup.select(column[field])
            result[count] = len(nodes)
            if field == 'heading_selector':
                result['heading_texts'] = [node.get_text(' ', strip=True)[:180] for node in nodes[:3]]
        except (SelectorSyntaxError, ValueError, NotImplementedError):
            result['selector_errors'].append(field)
    return result


def recognize_column(school_id, url, html, *, feedback=None):
    """Ask for executable selectors, retaining the failed trial on a repair turn."""
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.ai.skill_loader import canonical, digest, load_skill
    from backend.services.discovery_policy import get_policy
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
    handle = tasks.current_execution() or {}
    checkpoint = dict(handle.get('checkpoint') or {})
    material_hash = digest(regions)
    saved = checkpoint.get('column_regions', {})
    if saved.get('hash') != material_hash:
        saved = {'hash': material_hash, 'cursor': 0, 'columns': [], 'unresolved': [], 'decision_ids': []}
    skill = load_skill('university-source-onboarding', 'column')
    for index in range(saved['cursor'], len(regions)):
        evidence = regions[index]
        execution_id = 'column-region:' + digest([skill.resource_digest,
            evidence, binding['id'], binding['version']])
        try:
            result = run_skill('university-source-onboarding', 'column', evidence, 'directory', execution_id, binding=binding)
        except AIConfigError as exc:
            if exc.code == 'concurrency_limit' and handle:
                tasks.defer(capability='directory', delay=10, reason='等待 AI 空闲', checkpoint=checkpoint)
            columns = list({(c['name'], c['list_selector']): c for c in saved['columns']}.values())
            return {'status': 'ready' if columns else 'unsupported', 'columns': columns, 'reason':
                    '本轮 AI 用量已达上限，未完成入口已保留' if exc.code == 'budget_exhausted' else 'AI 辅助暂不可用，其他栏目继续接入',
                    'unresolved_regions': len(saved['unresolved']) + len(regions) - index,
                    'material_complete': False, 'remaining_regions': len(regions) - index,
                    'decision_ids': saved.get('decision_ids', []),
                    'error_code': exc.code}
        if result['status'] == 'pending' and handle:
            tasks.defer(capability='directory', delay=10, reason='等待已提交的栏目识别', checkpoint=checkpoint)
        if result['status'] != 'pending' and execution_id not in saved.setdefault('decision_ids', []):
            saved['decision_ids'].append(execution_id)
        if result['status'] == 'succeeded' and result['output']['status'] == 'ready':
            saved['columns'].extend(result['output']['columns'])
        elif result['status'] != 'succeeded' or result['output']['status'] == 'unsupported':
            saved['unresolved'].append(index)
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
    columns = list({(c['name'], c['list_selector']): c for c in saved['columns']}.values())
    return {'status': 'ready' if columns else 'unsupported' if saved['unresolved'] else 'not_column', 'columns': columns,
            'reason': '部分发布区域仍待识别' if saved['unresolved'] else '',
            'decision_ids': saved.get('decision_ids', []),
            'material_complete': True, 'remaining_regions': 0,
            'unresolved_regions': len(saved['unresolved'])}
