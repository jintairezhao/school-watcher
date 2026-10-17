"""Pure, explainable demand hints for ordering an observed discovery frontier.

Profiles describe interests, not administrative ownership or crawl permissions.
Only callers' observed branch assignments separate scheduling scopes. Neither a
name match nor a semantic score creates a source or removes a candidate.
"""
from collections.abc import Mapping
import json
import math
import re


CATEGORY_DEFINITIONS = {
    'academic_units': {'label': '院系与学科专业',
                       'pattern': r'院系|学院|学部|学系|教学单位|教学机构|学科|专业设置|专业介绍|专业建设'},
    'teaching': {'label': '教务与培养',
                 'pattern': r'教务|教学|培养|课程|选课|考务|学籍|学位|毕业|答辩|本科生院|研究生院'},
    'admissions': {'label': '招生与升学',
                   'pattern': r'招生|招考|考研|推免|保研|夏令营|推荐免试|复试|博士申请|硕士申请'},
    'funding': {'label': '奖学金与学生资助',
                'pattern': r'奖学金|助学|资助|奖助|勤工|困难补助|国家奖|学生贷款'},
    'career': {'label': '就业与实习',
               'pattern': r'就业|实习|职业发展|招聘会|校园招聘|双选会'},
    'competition_exchange': {'label': '竞赛与交流',
                             'pattern': r'竞赛|比赛|创新创业|国际交流|交换生|访学|留学|交流项目'},
    'research': {'label': '科研与学术',
                 'pattern': r'科研|学术|研究项目|课题|基金项目|科技处|科学技术|研究生|研讨会|讲座'},
    'personnel': {'label': '教师人事与发展',
                  'pattern': r'人事|人力资源|职称|师资|教师发展|教师工作|教职工|岗位聘任|人才招聘'},
    'student_services': {'label': '学生事务',
                         'pattern': r'学生工作|学生处|学生事务|学工|校园办事|住宿|心理健康|学生管理'},
}
CATEGORY_KEYS = tuple(CATEGORY_DEFINITIONS)
PROFILE_CATEGORIES = {
    'undergraduate': ('academic_units', 'teaching', 'admissions', 'funding',
                      'career', 'competition_exchange', 'student_services'),
    'graduate': ('academic_units', 'teaching', 'admissions', 'funding', 'career',
                 'competition_exchange', 'research', 'student_services'),
    'teacher': ('academic_units', 'teaching', 'research', 'personnel', 'competition_exchange'),
}
ALL_PROFILES = tuple(PROFILE_CATEGORIES)
CATEGORIES = CATEGORY_DEFINITIONS
PROFILE_NEEDS = PROFILE_CATEGORIES
_PATTERNS = {key: re.compile(value['pattern']) for key, value in CATEGORY_DEFINITIONS.items()}


def normalize_profiles(value=None):
    """Use the shared audience when unspecified; reject explicit misspellings."""
    if value is None or value == '' or value == [] or value == ():
        return ALL_PROFILES
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError('Discovery attention profiles must be named profiles')
    if any(not isinstance(profile, str) or profile not in PROFILE_CATEGORIES for profile in values):
        raise ValueError('Unknown discovery attention profile')
    return tuple(profile for profile in ALL_PROFILES if profile in values)


def _mapping(value):
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _path(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return ()
    return tuple(part for part in value if isinstance(part, str)) if isinstance(value, (list, tuple)) else ()


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _region(value):
    if isinstance(value, Mapping):
        return _text(value.get('container') or value.get('selector')
                     or value.get('container_locator') or value.get('list_selector'))
    return _text(value)


def complete_material(page):
    """Recognize a successful structural observation, not an HTTP shell.

    Inventory can retain a fetched fragment shell for adaptation. Its hash is
    genuine material identity but does not make the navigation complete.
    """
    page = _mapping(page)
    status, digest = page.get('status_code'), page.get('content_hash')
    return bool(page.get('state') == 'fetched'
                and isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)
                and not page.get('error')
                and (status is None or isinstance(status, int) and not isinstance(status, bool)
                     and 200 <= status < 300)
                and page.get('health') not in ('unreachable', 'dynamic_content', 'template_error',
                                              'needs_adapter', 'partial')
                and page.get('outcome') not in ('needs_adapter', 'requires_render', 'needs_manual')
                and page.get('error_code') != 'dynamic_material_incomplete')


def _columns(feed, url, *, locator='', region='', own=False):
    """A neighbouring list is never evidence about this particular target."""
    feed = _mapping(feed)
    values = feed.get('lists') if 'lists' in feed else [feed]
    if not isinstance(values, (list, tuple)):
        return ()
    values = [column for column in values if isinstance(column, Mapping)]
    candidates = [column for column in values if column.get('column_url') == url
                  or own and len(values) == 1 and not column.get('column_url')]
    if region:
        return tuple(column for column in candidates if region in (
            column.get('container_locator'), column.get('list_selector'), column.get('region_selector')))
    if locator:
        located = [column for column in candidates if column.get('column_link_locator') == locator]
        if located:
            return tuple(located)
    return tuple(candidates) if len(candidates) == 1 else ()


def need_evidence(page, *, incoming=(), parents=None, region=None):
    """Return interest hints from real labels, paths and precisely bound lists.

    Current, followed incoming edges can supply a strange-named target's local
    navigation context. Parent page identity is usable only inside a unit or
    publishing route; directory-wide labels and unrelated feed rows are not.
    Evidence remains data, and does not grant actions or prove ownership.
    """
    page, parents = _mapping(page), _mapping(parents)
    url, observations = _text(page.get('url')), []

    def observe(field, text, source_url=url, locator='', content_hash=''):
        text = _text(text)
        if text:
            observations.append({'field': field, 'text': text, 'url': source_url,
                                 'locator': _text(locator), 'content_hash': _text(content_hash)})

    def column_observations(column, source_url, content_hash):
        locator = column.get('container_locator') or column.get('list_selector') or ''
        observe('column_name', column.get('name'), source_url, locator, content_hash)
        samples = column.get('samples') or []
        for sample in samples if isinstance(samples, (list, tuple)) else ():
            if isinstance(sample, Mapping):
                observe('sample_title', sample.get('title'), source_url, locator, content_hash)

    for field in ('label', 'title'):
        observe(field, page.get(field), content_hash=page.get('content_hash', ''))
    for part in _path(page.get('path_json', page.get('path', []))):
        observe('path', part, content_hash=page.get('content_hash', ''))
    for column in (_columns(page.get('feed_json'), url, region=_region(region), own=True)
                   if complete_material(page) else ()):
        column_observations(column, url, page.get('content_hash', ''))
    for raw_edge in incoming or ():
        edge = _mapping(raw_edge)
        parent = _mapping(parents.get(edge.get('parent_url')))
        if (edge.get('target_url') != url or not url
                or edge.get('decision') not in ('follow', 'official_external_link')
                or not complete_material(parent)
                or edge.get('content_hash') != parent.get('content_hash')):
            continue
        parent_url, content_hash = edge.get('parent_url', ''), parent['content_hash']
        observe('edge_label', edge.get('label'), parent_url, edge.get('locator'), content_hash)
        for part in _path(edge.get('path_json', edge.get('path', []))):
            observe('edge_path', part, parent_url, edge.get('locator'), content_hash)
        if parent.get('kind') in ('unit', 'gateway', 'channel'):
            for field in ('label', 'title'):
                observe('parent_' + field, parent.get(field), parent_url, edge.get('locator'), content_hash)
        for column in _columns(parent.get('feed_json'), url, locator=_text(edge.get('locator'))):
            column_observations(column, parent_url, content_hash)
    evidence = []
    seen = set()
    for observation in observations:
        for category, pattern in _PATTERNS.items():
            matches = pattern.finditer(observation['text'])
            # A programme already reviewed as a major is not the HR office.
            # This only removes that ambiguous interest hint, never the route.
            match = next((match for match in matches if not (
                page.get('kind') == 'major' and category == 'personnel'
                and match.group() == '人力资源')), None)
            if not match:
                continue
            item = dict(observation, category=category, match=match.group())
            identity = tuple(item.items())
            if identity not in seen:
                evidence.append(item)
                seen.add(identity)
    needs = tuple(category for category in CATEGORY_KEYS
                  if any(item['category'] == category for item in evidence))
    return {'needs': needs, 'evidence': evidence}


def _scope(url, branches):
    """Observed graph identity only; identical names never merge branches."""
    return _text(branches.get(url)) or url if url else None


def coverage_state(pages, *, branches=None, validated_sources=(), edges=()):
    """Separate successful material probes from verified collecting sources.

    ``validated_sources`` is supplied by the collector, never the model. A
    receipt needs stable source identity, rule hash, material hash and concrete
    region, and must still bind to that retained page material. Keeping the
    material through a later network failure preserves its valid receipt.
    Counts are local scope/category observations, not full-school percentages.
    """
    pages, branches = _mapping(pages), _mapping(branches)
    incoming = {}
    for edge in edges or ():
        if isinstance(edge, Mapping):
            incoming.setdefault(edge.get('target_url'), []).append(edge)
    cells = {}

    def cell(scope, category):
        return cells.setdefault(scope, {}).setdefault(category, {
            'probes': [], 'validated': [], 'validation_records': []})

    for address, raw_page in pages.items():
        page = _mapping(raw_page)
        url = _text(page.get('url')) or _text(address)
        scope = _scope(url, branches)
        if not scope:
            continue
        hints = need_evidence(dict(page, url=url), incoming=incoming.get(url, ()), parents=pages)
        for category in hints['needs']:
            current = cell(scope, category)
            if complete_material(page):
                probe = {'url': url, 'content_hash': page['content_hash']}
                if probe not in current['probes']:
                    current['probes'].append(probe)
    for raw_receipt in validated_sources or ():
        receipt = _mapping(raw_receipt)
        url = _text(receipt.get('url'))
        page = _mapping(pages.get(url))
        source_id, region = receipt.get('source_id'), _region(receipt.get('region'))
        if (not isinstance(source_id, (int, str)) or isinstance(source_id, bool) or not str(source_id).strip()
                or not _text(receipt.get('config_hash')) or not _text(receipt.get('content_hash')) or not region
                or receipt.get('content_hash') != page.get('content_hash')
                or receipt.get('status', 'validated') not in ('validated', 'passed', 'success')):
            continue
        scope = _scope(url, branches)
        if not scope:
            continue
        selected = _columns(page.get('feed_json'), url, region=region, own=True)
        feed = _mapping(page.get('feed_json'))
        rows = feed.get('lists') if 'lists' in feed else [feed]
        multiple_lists = isinstance(rows, (list, tuple)) and len(rows) > 1
        region_record = _mapping(receipt.get('region'))
        region_name = _text(region_record.get('name'))
        if region_name:
            # The collector executed this exact heading/container on this
            # snapshot. ``heading`` itself is a locator, never title text.
            # Its containing section can be wider than feed.container_locator;
            # do not guess CSS ancestry or borrow a neighbouring list's rows.
            local_page = dict(page, url=url, label=region_name, title='', path_json='[]',
                              feed_json={'lists': list(selected)} if selected else None)
            hints = need_evidence(local_page, region=region)
        elif selected:
            # Concrete selected rows outrank whole-page or incoming context.
            # A shared URL can expose both aid and personnel modules at once.
            local_page = dict(page, url=url, label='', title='', path_json='[]',
                              feed_json={'lists': list(selected)})
            hints = need_evidence(local_page, region=region)
        elif multiple_lists:
            # Receipt identity is still valid; its demand category is unknown.
            # Do not invent coverage from a different named region.
            continue
        else:
            hints = need_evidence(dict(page, url=url), incoming=incoming.get(url, ()),
                                  parents=pages, region=region)
        # A receipt for one list never borrows another list's sample titles.
        for category in hints['needs']:
            current = cell(scope, category)
            identity = str(source_id)
            if identity not in current['validated']:
                current['validated'].append(identity)
            record = {key: receipt[key] for key in ('source_id', 'config_hash', 'content_hash')}
            record.update(url=url, region=dict(region_record) if region_record else region)
            if record not in current['validation_records']:
                current['validation_records'].append(record)
    return {'cells': cells}


def candidate_attention(page, *, profiles=None, weights=None, coverage=None,
                        incoming=(), parents=None, branches=None):
    """A deterministic preference, never a probability or eligibility verdict.

    Weight defaults are neutral, with production policy supplied by the caller.
    Validated local sources diminish marginal scheduling benefit. Attempts,
    network errors and successful probes alone do not reduce semantic utility.
    """
    profiles = normalize_profiles(profiles)
    supplied = {} if weights is None else weights
    if not isinstance(supplied, Mapping) or any(
            category not in CATEGORY_DEFINITIONS or isinstance(weight, bool)
            or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0
            for category, weight in supplied.items()):
        raise ValueError('Attention weights must map known categories to positive finite numbers')
    active = {category for profile in profiles for category in PROFILE_CATEGORIES[profile]}
    page = _mapping(page)
    hints = need_evidence(page, incoming=incoming, parents=parents)
    scope = _scope(_text(page.get('url')), _mapping(branches))
    covered = _mapping(_mapping(_mapping(coverage).get('cells')).get(scope))
    gains = {}
    coverage_detail = {}
    for category in hints['needs']:
        category_cell = _mapping(covered.get(category))
        validated = category_cell.get('validated') or []
        count = len(set(str(value) for value in validated)) if isinstance(validated, (list, tuple, set)) else 0
        probes = category_cell.get('probes') or []
        probe_count = len(probes) if isinstance(probes, (list, tuple)) else 0
        coverage_detail[category] = {'probed': probe_count, 'validated': count,
                                     'state': 'validated' if count else 'probed' if probe_count else 'unexplored'}
        if category in active:
            gains[category] = float(supplied.get(category, 1)) / (1 + count)
    score = sum(gains.values())
    return {'rank': (-score,), 'score': score, 'needs': hints['needs'], 'scope': scope,
            'profiles': profiles, 'evidence': hints['evidence'], 'gains': gains,
            'coverage': coverage_detail,
            'reason': 'observed_demand_gap' if gains else 'unclassified_or_other_interest_retained'}
