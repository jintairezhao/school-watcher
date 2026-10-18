"""Read-only discovery telemetry. Never fetch a website or dispatch a model here."""
from datetime import datetime
import hashlib
import json
import logging
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.database.models import BackgroundTask, Department
from backend.database.source_governance_models import SourceProposal
from backend.scraper.discovery.attention import CATEGORY_DEFINITIONS, need_evidence
from backend.services.discovery_policy import get_attention_policy
from backend.services.source_inventory import canonical_url, site_key

logger = logging.getLogger(__name__)
KINDS = {'root': '学校官网', 'directory': '目录导航', 'unit': '院系部门',
         'major': '学科专业', 'channel': '信息栏目', 'navigation': '导航入口'}
STATES = {'connected': '已接入', 'running': '正在处理', 'pending': '等待探索',
          'fetched': '已读取', 'failed': '等待恢复', 'waiting': '等待继续',
          'observed': '已发现入口'}
PHASES = {'fetch': '读取官网', 'crawl': '探索网站结构', 'ai': 'AI 识别入口',
          'ai_resource': '等待 AI 空闲', 'ai_connection': '等待连接恢复',
          'ai_result': '等待确认请求结果', 'ai_budget': '等待调整额度',
          'render': '等待动态页面', 'verification': '等待访问验证',
          'user_paused': '用户已暂停', 'complete': '处理结束'}
ERRORS = {'provider_connection_paused': 'AI 连接连续失败，等待连接恢复',
          'ai_result_unknown': '付费请求结果尚未确认，未自动重发',
          'budget_exhausted': '已达到探索额度，进度已保存',
          'timeout': '页面读取超时，等待恢复',
          'output_validation_failed': '模型结果尚未通过检查',
          'invalid_json': '模型返回格式未通过检查'}


def _object(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or '{}')
    except (ValueError, TypeError):
        return {} if default is None else default


def _url(value):
    try:
        url = canonical_url(value) if isinstance(value, str) else ''
        parts = urlsplit(url)
        # A diagnostic URL must never publish credentials embedded in a query.
        query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                 if key.lower() not in ('token', 'access_token', 'api_key', 'apikey', 'key', 'secret', 'password')]
        return urlunsplit(parts._replace(query=urlencode(query))) if url else ''
    except (ValueError, UnicodeError):
        return ''


def _stamp(value):
    return value.isoformat() + 'Z' if isinstance(value, datetime) else value


def _name(value, fallback='未命名官网入口'):
    from backend.services.source_labels import source_label
    if not isinstance(value, str):
        return fallback
    return source_label(value, fallback)[:200]


def _path(value):
    parts = _object(value, []) if isinstance(value, str) else value
    return [_name(part) for part in parts[:16] if isinstance(part, str) and part.strip()] if isinstance(parts, list) else []


def _task(row, now):
    payload, checkpoint = row.payload or {}, row.checkpoint or {}
    progress = checkpoint.get('discovery_progress') or {}
    page = payload.get('page') or checkpoint.get('onboarding_page') or checkpoint.get('discovery_active_page') or {}
    url = _url(page.get('url') or payload.get('url') or progress.get('current_url'))
    state = row.state
    if state == 'running' and (not row.lease_until or row.lease_until <= now):
        state = 'waiting'
    reason = ERRORS.get(row.error_code)
    if not reason and row.state == 'running' and state == 'waiting':
        reason = '等待任务恢复，上次工作租约已失效'
    elif not reason and state == 'pending':
        reason = '等待调度' if row.available_at <= now else '已安排稍后继续'
    elif not reason and state in ('done', 'failed'):
        reason = '本项处理结束' if state == 'done' else '本项未完成，进度已保存'
    elif not reason and state == 'waiting':
        reason = PHASES.get(row.phase) if row.phase in ('ai_resource', 'ai_connection', 'ai_result',
            'ai_budget', 'verification', 'user_paused', 'render') else '等待继续'
    elif not reason:
        reason = PHASES.get(row.phase)
    if state == 'running' and payload.get('discovery_pause_requested'):
        reason = '已请求暂停，等待当前处理完成'
    if not reason:
        reason = {'pending': '等待调度', 'running': '正在处理', 'waiting': '等待继续',
                  'done': '本项处理结束', 'failed': '本项未完成，进度已保存'}.get(state, '已记录')
    return {'id': row.id, 'generation': row.generation, 'kind': row.kind, 'state': state,
            'phase': row.phase, 'reason': reason, 'url': url,
            'label': _name(page.get('label') or progress.get('current_label'),
                           {'discover': '官网结构探索', 'navigation_review': '官网入口识别',
                            'onboard': '栏目验证与接入'}.get(row.kind, '探索任务')),
            'path': _path(page.get('path_json') or page.get('path')),
            'updated_at': _stamp(row.updated_at), 'available_at': _stamp(row.available_at),
            'error_code': row.error_code if row.error_code in ERRORS else ''}


def snapshot(school):
    from backend.services.onboarding_progress import status
    from backend.services.runtime_catalog import runtime_catalog
    from backend.services.discovery_costs import load_discovery_costs
    now = datetime.utcnow()
    progress = status(school)
    catalog_error = False
    try:
        catalog = runtime_catalog()
        report = catalog.monitor_snapshot(site_key(school.url)) or {}
    except (OSError, sqlite3.Error, RuntimeError):
        logger.exception('Discovery monitor could not read the published catalogue')
        report, catalog_error = {}, True
    pages = {}
    for original in report.get('pages', []):
        url = _url(original.get('url'))
        if url:
            pages[url] = dict(original, url=url)
    parent = BackgroundTask.query.filter_by(identity=f'discover:{school.id}').first()
    query = BackgroundTask.query.filter(BackgroundTask.kind.in_(('discover', 'navigation_review', 'onboard')),
        BackgroundTask.payload['school_id'].as_integer() == school.id)
    # A previous navigation generation is history, not current work.
    rows = query.order_by(BackgroundTask.updated_at.desc(), BackgroundTask.id.desc()).limit(150).all()
    tasks = [_task(row, now) for row in rows if row.kind != 'navigation_review' or not parent or
             (row.payload or {}).get('school_generation', (row.payload or {}).get('parent_generation')) == parent.generation]
    task_pages = {}
    for task in tasks:
        if task['url']:
            task_pages.setdefault(task['url'], []).append(task)
            pages.setdefault(task['url'], dict(url=task['url'], label=task['label'],
                path_json=task['path'], kind='navigation', state='observed'))
    # All concurrent crawl targets are recorded by the worker, not guessed from labels.
    active = ((parent.checkpoint or {}).get('discovery_progress') or {}) if parent else {}
    for page in active.get('current_pages', []) if parent and parent.state == 'running' and parent.lease_until and parent.lease_until > now else []:
        url = _url(page.get('url'))
        if url:
            pages.setdefault(url, dict(page, url=url, state='observed', kind='navigation'))
            task_pages.setdefault(url, []).append(dict(id=parent.id, state='running',
                reason='正在探索网站结构', updated_at=_stamp(parent.updated_at)))
    sources = {}
    for row in Department.query.filter_by(school_id=school.id).order_by(Department.id):
        url = _url(row.list_url)
        if url and row.list_selector:
            sources.setdefault(url, []).append({'id': row.id, 'name': _name(row.name, '已接入栏目'),
                                               'group': _name(row.group_name, '')})
            pages.setdefault(url, dict(url=url, label=row.name, kind='channel', state='observed'))
    nodes = []
    for url, page in pages.items():
        current = task_pages.get(url, [])
        live = [item for item in current if item['state'] in ('running', 'pending', 'waiting')]
        failed = (page.get('state') == 'failed' or page.get('health') == 'unreachable'
                  or any(item['state'] == 'failed' for item in current))
        state = ('running' if any(item['state'] == 'running' for item in live) else
                 'connected' if url in sources else 'waiting' if any(item['state'] == 'waiting' for item in live) else
                 'pending' if any(item['state'] == 'pending' for item in live) else
                 'failed' if failed else page.get('state', 'observed'))
        if state not in STATES:
            state = 'observed'
        needs = need_evidence(page).get('needs', [])
        nodes.append({'id': hashlib.sha256(url.encode()).hexdigest()[:20], 'url': url,
            'label': _name(page.get('label') or page.get('title')), 'kind': page.get('kind', 'navigation'),
            'kind_label': KINDS.get(page.get('kind'), '官网入口'), 'state': state, 'state_label': STATES[state],
            'path': _path(page.get('path_json')), 'checked_at': page.get('checked_at'),
            'status_code': page.get('status_code'), 'sources': sources.get(url, []),
            'tasks': current[:6], 'categories': list(needs),
            'reason': live[0]['reason'] if live else '栏目配置已接入，可持续更新' if url in sources else
                      '读取未完成，等待后台恢复' if failed else
                      '页面已读取，仍需识别和验证具体栏目' if state == 'fetched' else '已记录官网入口',
            'path_basis': '观察到的导航路径' if _path(page.get('path_json')) else '导航路径未记录'})
    rank = {'running': 0, 'failed': 1, 'waiting': 2, 'connected': 3}
    nodes.sort(key=lambda item: (rank.get(item['state'], 4), item['label'], item['url']))
    total = len(nodes)
    policy = get_attention_policy(school.id)
    directions = [{'id': key, 'label': value['label'],
                   'observed': sum(key in node['categories'] for node in nodes),
                   'connected': sum(len(node['sources']) for node in nodes if key in node['categories'])}
                  for key, value in CATEGORY_DEFINITIONS.items()]
    proposals = SourceProposal.query.filter_by(school_id=school.id).filter(
        SourceProposal.state.in_(('proposed', 'validating', 'validated', 'needs_review'))).count()
    cost = load_discovery_costs({'name': school.name, 'root_url': school.url})
    page_costs = {_url(url): value for url, value in cost['by_page'].items()}
    for node in nodes:
        node['cost'] = page_costs.get(node['url'])
    timeline = [{'id': f"task:{task['id']}", 'at': task['updated_at'], 'label': task['label'],
                 'detail': task['reason'], 'state': task['state'], 'url': task['url']} for task in tasks[:16]]
    public_progress = {key: progress.get(key) for key in ('state', 'message', 'stage_message',
        'busy', 'active', 'current_label', 'updated_at', 'checked_pages', 'pending_pages',
        'verified_source_count', 'column_tasks', 'can_pause', 'can_resume', 'ai_cached_routes', 'ai_message')}
    public_progress['current_label'] = _name(progress.get('current_label'), '')
    from backend.services.localization import school_context
    return {'school': {'id': school.id, 'name': school.name, 'url': _url(school.url),
                       'localization':school_context(school)},
        'observed_at': _stamp(now), 'progress': public_progress,
        'catalog': {'published_at': report.get('catalogue_published_at'), 'unavailable': catalog_error,
                    'historical': True, 'total_nodes': total, 'shown_nodes': min(total, 1200),
                    'truncated': total > 1200},
        'nodes': nodes[:1200], 'tasks': tasks[:40], 'recent': timeline,
        'pending_candidates': proposals, 'directions': directions,
        'profiles': list(policy['profiles']), 'profile_context': policy['profile_context'], 'cost': {key: cost[key] for key in (
            'scope', 'totals', 'ledger_gap', 'attribution_complete', 'unattributed')},
        'coverage_complete': False, 'refresh_seconds': 3}
