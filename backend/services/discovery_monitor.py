"""Read-only discovery telemetry. Never fetch a website or dispatch a model here."""
from datetime import datetime
import hashlib
import json
import logging
import re
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.database.models import BackgroundTask, Department, DepartmentDirectoryEntry
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
            'page_label': _name(page.get('label'), ''),
            'label': _name(page.get('label') or progress.get('current_label'),
                           {'discover': '官网结构探索', 'navigation_review': '官网入口识别',
                            'onboard': '栏目验证与接入'}.get(row.kind, '探索任务')),
            'path': _path(page.get('path_json') or page.get('path')),
            'updated_at': _stamp(row.updated_at), 'available_at': _stamp(row.available_at),
            'error_code': row.error_code if row.error_code in ERRORS else ''}


def _project_directory_relations(school, pages, departments, *, catalogue_available):
    """Read saved memberships and evidenced source placements, never infer by name.

    A crawl's discovery path is not the source's ownership path. Preserve that
    original path separately; only project an unambiguous existing relationship.
    """
    from backend.services.source_placements import official_source_placements
    by_id = {row.id: row for row in departments}
    installed = [row for row in departments if row.list_selector and _url(row.list_url)]
    if not installed:
        return []
    placements = {}
    if catalogue_available:
        try:
            placements = official_source_placements(installed)
        except (OSError, sqlite3.Error, RuntimeError):
            logger.exception('Discovery monitor could not read published source placements')
    parents = {}
    for entry in DepartmentDirectoryEntry.query.filter(
            DepartmentDirectoryEntry.parent_id.in_(by_id),
            DepartmentDirectoryEntry.department_id.in_(by_id)).all():
        parent = by_id[entry.parent_id]
        if parent.kind in ('group', 'unit'):
            parents.setdefault(entry.department_id, []).append(parent.id)

    def chains(ident, seen=()):
        if ident in seen or len(seen) >= 16:
            return []
        candidates = parents.get(ident, [])
        if not candidates:
            return [[]]
        return [prefix + [by_id[parent]] for parent in sorted(candidates)
                for prefix in chains(parent, seen + (ident,))]

    projected = {}

    def propose(url, trail, label, kind, basis):
        url = _url(url)
        if not url or url.rstrip('/') == _url(school.url).rstrip('/'):
            return
        projected.setdefault(url, []).append(dict(path=[school.name] + [n['name'] for n in trail],
            label=label, kind=kind, basis=basis, parent_url=trail[-1]['url'] if trail else ''))

    for source in installed:
        choices = []
        for chain in chains(source.id):
            if chain:
                choices.append([{'name': _name(row.name), 'url': _url(row.list_url),
                                 'kind': 'directory' if row.kind == 'group' else 'unit'} for row in chain])
        if not choices:
            for placement in placements.get(source.id, []):
                group = placement.get('group')
                if not group or not placement.get('nodes'):
                    continue
                # A matching stored group supplies its real page URL; absent URLs
                # stay named path groups rather than invented fetched pages.
                groups = [row for row in departments if row.kind == 'group' and row.name == group]
                address = _url(groups[0].list_url) if len(groups) == 1 else ''
                choices.append([{'name': _name(group), 'url': address, 'kind': 'directory'}] +
                    [{'name': _name(n['name']), 'url': _url(n.get('url')), 'kind': 'unit'}
                     for n in placement['nodes']])
        unique = {tuple((n['name'], n['url'], n['kind']) for n in chain): chain for chain in choices}
        if len(unique) != 1:
            continue  # Conflicting owners must not be resolved by iteration order.
        trail = next(iter(unique.values()))
        basis = '已保存的目录归属' if parents.get(source.id) else '官网目录与栏目入口记录'
        for index, node in enumerate(trail):
            propose(node['url'], trail[:index], node['name'], node['kind'], basis)
        url = _url(source.list_url)
        if trail[-1]['url'] == url:
            propose(url, trail[:-1], trail[-1]['name'], trail[-1]['kind'], basis)
            continue
        page = pages.get(url, {})
        label = _name(page.get('label') or source.name)
        owners = {n['name'] for n in trail}
        labels = {_name(p['label']) for p in placements.get(source.id, []) if p.get('label')}
        if len(labels) == 1 and (label in owners or label == _name(source.name)):
            label = next(iter(labels))
        propose(url, trail, label, 'channel', basis)

    relations = []
    for url, proposals in projected.items():
        paths = {tuple(p['path']) for p in proposals}
        labels = {p['label'] for p in proposals}
        if len(paths) != 1 or len(labels) != 1:
            continue
        projection = proposals[0]
        page = pages.setdefault(url, dict(url=url, state='observed'))
        page.setdefault('navigation_path', _path(page.get('path_json')))
        page.update(path_json=projection['path'], label=projection['label'],
                    kind=projection['kind'], path_basis=projection['basis'])
        parent_urls = {_url(p['parent_url']) for p in proposals}
        if len(parent_urls) == 1 and (parent_url := next(iter(parent_urls))) and parent_url != url:
            relations.append(dict(source_url=parent_url, target_url=url, type='directory',
                evidence_url=parent_url, locator='', basis=projection['basis']))
    return relations


def _navigation_relations(records, pages):
    """Only a URL-bearing parent in the same observed document establishes a link.

    Menu links describe website navigation, never organisational subordination.
    A name path, shared label, or similar URL cannot establish a relationship.
    """
    by_reference = {}
    for node in records:
        reference = _url(node.get('reference_url'))
        if reference:
            by_reference.setdefault(reference, []).append(node)
    allowed = {'menu_group', 'navigation_entry', 'directory_entry', 'unit_channel',
               'publication_column', 'website_entrance', 'hidden_directory_entry'}
    relations = []
    for reference, nodes in by_reference.items():
        by_key = {}
        for node in nodes:
            by_key.setdefault(node.get('node_key'), []).append(node)
        for node in nodes:
            target = _url(node.get('url'))
            if node.get('relation') not in allowed or not target or not node.get('parent_key'):
                continue
            parents = by_key.get(node['parent_key'], [])
            addresses = {_url(n.get('url')) for n in parents if n.get('url')}
            if len(addresses) != 1:
                continue
            source = next(iter(addresses))
            if any(n.get('relation') == 'page_identity' for n in parents):
                source = reference  # Identity URLs can be the HTTPS final address.
            if not source or source == target or source not in pages:
                continue
            pages.setdefault(target, dict(url=target, label=_name(node.get('name'), target),
                kind='navigation', state='observed'))
            directory = node['relation'] in {'directory_entry', 'hidden_directory_entry'} and pages[source].get('kind') == 'directory'
            relations.append(dict(source_url=source, target_url=target, type='directory' if directory else 'navigation',
                evidence_url=reference, locator=str(node.get('locator') or '')[:500],
                basis='官网目录中的入口记录' if directory else '官网页面中记录的导航链接'))
    return relations


def _graph_scope(school_url, pages, relations, records, sources, task_urls=()):
    """Project saved observations onto this school's meaningful website structure.

    Discovery history includes outbound links, articles and application tools.
    Their presence is not evidence that they are part of the school's framework.
    This projection changes neither the catalogue nor installed source identities.
    """
    root = _url(school_url)
    host = (urlsplit(root).hostname or '').rstrip('.').removeprefix('www.')

    def local(url):
        address = (urlsplit(url).hostname or '').rstrip('.')
        return bool(host) and (address == host or address.endswith('.' + host))

    # Only current records delivered by monitor_snapshot are used. In particular,
    # an external page's backlink cannot establish an internal structural entry.
    incoming, own = {}, {}
    for record in records:
        url, reference = _url(record.get('url')), _url(record.get('reference_url'))
        if not url or not reference or not local(reference):
            continue
        if record.get('relation') == 'page_identity':
            own.setdefault(reference, []).append(record)
        else:
            incoming.setdefault(url, []).append(record)
    roster = {'directory_entry', 'hidden_directory_entry', 'nested_directory_entry',
              'sub_unit', 'attached_unit', 'major_directory_entry', 'programme_college',
              'unit_profile_entry'}
    menus = {'menu_group', 'navigation_entry', 'unit_channel', 'publication_column',
             'website_entrance'}
    directory_label = re.compile(r'院系设置|院系部门|学科设置|学科目录|专业设置|专业介绍|专业目录|'
        r'机构设置|组织机构|机构职能|教学机构|\b(?:academic units|schools and departments|'
        r'faculties and departments|departments?|faculties|colleges?|schools|administration|'
        r'organizational structure|organisational structure|degree programs?|academic programmes?)\b', re.I)
    utility_label = re.compile(r'^(?:电子邮件|邮箱|邮件系统|个人中心|我的图书馆|登录|登陆|退出|'
        r'校内门户|站内搜索|搜索|网站地图|校园地图|设为首页|加入收藏|在线投稿|后台管理|'
        r'网站管理|旧版网站|返回首页|English|中文版|简体中文|繁体中文|'
        r'sign in|log ?in|log ?out|webmail|e-?mail|search|site ?map|my account|'
        r'my library|privacy policy|cookie policy|accessibility statement)$', re.I)
    utility_url = re.compile(r'\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|jpe?g|png|gif|mp4)$|'
        r'/(?:downloadfile|login|logout|webmail|sso|search)(?:[/.]|$)', re.I)
    article_url = re.compile(r'/(?:c|content|article|articles|detail|details|infoshow)/|'
        r'/(?:[^/]*details?|showcontent|infoshow|viewarticle|readnews|shownews|articleview)(?:[/.?]|$)|'
        r'/info/\d+/\d+\.[a-z]+(?:$|\?)|'
        r'/(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}(?:/|[-_])', re.I)
    dated_label = re.compile(r'^\s*[【\[(]?(?:19|20)\d{2}[-年/.]\d{1,2}(?:[-月/.]\d{1,2})?')
    omitted = dict(external=0, article=0, utility=0, unidentified=0, nonstructural=0)
    kept = {}
    for url, page in pages.items():
        label = _name(page.get('label') or page.get('title'), '')
        kind = page.get('kind')
        proofs = incoming.get(url, [])
        roster_proof = any(record.get('relation') in roster for record in proofs)
        directory_proof = kind == 'directory' and bool(directory_label.search(label)) and bool(
            page.get('content_hash') or own.get(url) or roster_proof)
        # This field is set only by the verified saved-directory projection above.
        # A generic observed name path never exempts an article from filtering.
        structural_proof = roster_proof or directory_proof or page.get('path_basis') in (
            '已保存的目录归属', '官网目录与栏目入口记录')
        is_root = url.rstrip('/') == root.rstrip('/')
        reason = None
        if not local(url):
            reason = 'external'
        elif not is_root and (utility_label.fullmatch(label) or utility_url.search(urlsplit(url).path)):
            reason = 'utility'
        elif not is_root and not structural_proof and url not in sources and (
                article_url.search(url) or dated_label.search(label)):
            reason = 'article'
        elif not is_root and not label:
            reason = 'unidentified'
        else:
            # A generic task URL or a plain menu label is not a structural node.
            # Relevance is read from this entry's own label, not an inherited
            # school/college path that would make every footer link relevant.
            interests = need_evidence({'url': url, 'label': label}).get('needs', [])
            observed_menu = any(record.get('relation') in menus for record in proofs)
            # Legacy classifiers also called policy folders such as "教育部"
            # units. Without a directory membership, require a relevant unit
            # name or the recorded identity of a department website entrance.
            entrance = (len([part for part in urlsplit(url).path.split('/') if part]) <= 1
                        and bool(own.get(url)))
            recorded_structure = (kind in ('directory', 'major', 'channel') or
                                  kind == 'unit' and bool(interests or entrance))
            meaningful = (is_root or url in sources or structural_proof or
                recorded_structure or
                bool(interests and (observed_menu or url in task_urls)))
            if not meaningful:
                reason = 'nonstructural'
        if reason:
            omitted[reason] += 1
            continue
        basis, priority = (('school_root', 0) if is_root else
            ('configured_source', 1) if url in sources else
            ('directory_evidence', 2) if structural_proof else
            ('recorded_structure', 3) if kind in ('directory', 'unit', 'major', 'channel') else
            ('observed_navigation', 4))
        kept[url] = dict(page, scope_basis=basis, structure_priority=priority)
    links = [relation for relation in relations
             if relation['source_url'] in kept and relation['target_url'] in kept
             and local(_url(relation.get('evidence_url')))]
    return kept, links, dict(mode='school_structure', root_domain=host,
        observed_nodes=len(pages), included_nodes=len(kept), omitted_nodes=sum(omitted.values()),
        omitted=omitted, omitted_relations=len(relations) - len(links))


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
            pages.setdefault(task['url'], dict(url=task['url'], label=task['page_label'] or task['url'],
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
    departments = Department.query.filter_by(school_id=school.id).order_by(Department.id).all()
    for row in departments:
        url = _url(row.list_url)
        if url and row.list_selector:
            sources.setdefault(url, []).append({'id': row.id, 'name': _name(row.name, '已接入栏目'),
                                               'group': _name(row.group_name, '')})
            pages.setdefault(url, dict(url=url, label=row.name, kind='channel', state='observed'))
    relations = _navigation_relations(report.get('structure_nodes', []), pages)
    relations += _project_directory_relations(school, pages, departments, catalogue_available=not catalog_error)
    pages, relations, graph_scope = _graph_scope(school.url, pages, relations,
        report.get('structure_nodes', []), sources, task_pages)
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
        label = _name(page.get('label') or page.get('title'), '')
        nodes.append({'id': hashlib.sha256(url.encode()).hexdigest()[:20], 'url': url,
            'label': label or url, 'label_source': 'recorded' if label else 'url', 'kind': page.get('kind', 'navigation'),
            'kind_label': KINDS.get(page.get('kind'), '官网入口'), 'state': state, 'state_label': STATES[state],
            'path': _path(page.get('path_json')), 'checked_at': page.get('checked_at'),
            'status_code': page.get('status_code'), 'sources': sources.get(url, []),
            'tasks': current[:6], 'categories': list(needs),
            'reason': live[0]['reason'] if live else '栏目配置已接入，可持续更新' if url in sources else
                      '读取未完成，等待后台恢复' if failed else
                      '页面已读取，仍需识别和验证具体栏目' if state == 'fetched' else '已记录官网入口',
            'navigation_path': page.get('navigation_path'),
            'path_basis': page.get('path_basis') or ('观察到的导航路径' if _path(page.get('path_json')) else '导航路径未记录')})
        nodes[-1].update(scope_basis=page['scope_basis'], structure_priority=page['structure_priority'])
    rank = {'running': 0, 'failed': 1, 'waiting': 2, 'connected': 3}
    nodes.sort(key=lambda item: (item['kind'] != 'root', rank.get(item['state'], 4),
        item['structure_priority'], {'directory': 0, 'unit': 1, 'channel': 2}.get(item['kind'], 3), item['label'], item['url']))
    total = len(nodes)
    shown = nodes[:1200]
    by_url = {node['url']: node['id'] for node in shown}
    links, seen = [], set()
    for relation in relations:
        source, target = by_url.get(relation.pop('source_url')), by_url.get(relation.pop('target_url'))
        identity = (source, target, relation['type'])
        if not source or not target or identity in seen:
            continue
        seen.add(identity)
        links.append(dict(relation, source=source, target=target))
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
                    'truncated': total > 1200, 'scope': graph_scope},
        'nodes': shown, 'relations': links, 'tasks': tasks[:40], 'recent': timeline,
        'pending_candidates': proposals, 'directions': directions,
        'profiles': list(policy['profiles']), 'profile_context': policy['profile_context'], 'cost': {key: cost[key] for key in (
            'scope', 'totals', 'ledger_gap', 'attribution_complete', 'unattributed')},
        'coverage_complete': False, 'refresh_seconds': 3}
