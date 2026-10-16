"""Explainable ordering of observed routes, with a durable exploration turn.

Ranks are queue preferences, never probabilities, ownership verdicts or reasons
to erase an entrance. Transport failures supply no semantic ranking evidence.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlsplit

PUBLISHING = re.compile(r'通知|公告|公示|招生|学术活动|办事指南|工作安排|政策|规章|制度|下载')


def _today():
    return datetime.now(timezone.utc).date()


def _feed(value):
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or '{}')
    except (TypeError, ValueError):
        return {}


def publication_date(feed):
    """Use dates of actual list items, never a footer or page-wide date."""
    feed = _feed(feed)
    dates = []
    for column in feed.get('lists') or [feed]:
        values = [sample.get('date') for sample in column.get('samples', [])]
        if column.get('publication_dates_complete') and column.get('dated_item_count', 0) > 0:
            values.append(column.get('latest_publication'))
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                dates.append(datetime.fromisoformat(value).date())
            except ValueError:
                pass
    return max(dates) if dates else None


def old_publications(feed, *, today=None):
    latest = publication_date(feed)
    # Match the existing infrequent/stale observation's one-year boundary.
    # This asks for neighboring evidence; it never disables an old source.
    return bool(latest and latest < (today or _today()) - timedelta(days=365))


def local_navigation(node):
    """Actual local menus/cards/images, excluding chrome and breadcrumbs."""
    parents = [node, *node.parents]
    for parent in parents:
        if not hasattr(parent, 'get'):
            continue
        identity = ' '.join(parent.get('class', [])) + ' ' + parent.get('id', '')
        if parent.name in ('header', 'footer') or re.search(r'header|footer|breadcrumb|crumb', identity, re.I):
            return False
    return any(hasattr(parent, 'get') and (
        parent.name in ('nav', 'aside') or parent.get('role') == 'navigation'
        or re.search(r'nav|menu|sidebar|sidenav|submenu|category|topic|card|banner|feature|zhuanti',
                     ' '.join(parent.get('class', [])) + ' ' + parent.get('id', ''), re.I)
        or parent.name in ('h2', 'h3')) for parent in parents) or (
            node.find('img') is not None
            and any(parent.name in ('main', 'section', 'article') for parent in parents))


def observed_label(node):
    """Use real anchor/image text; keep an unlabeled observed address unlabeled."""
    label = node.get_text(' ', strip=True) or node.get('title') or node.get('aria-label')
    image = node.find('img')
    return (label or (image.get('alt') or image.get('title') if image else '') or '').strip()


def article_addresses(feed):
    """All known list item addresses, including opaque CMS article paths."""
    feed = _feed(feed)
    result = set()
    for column in [feed, *(feed.get('lists') or [])]:
        result.update(value for value in column.get('article_urls', []) if isinstance(value, str))
        result.update(sample['url'] for sample in column.get('samples', [])
                      if isinstance(sample, dict) and sample.get('url'))
    return result


def utility_route(url, label=''):
    """Observed sign-in handoffs and explicitly named office utilities."""
    from backend.scraper.auth_routes import is_sign_in_route
    from .structure import SERVICE
    segment = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1].rsplit('.', 1)[0]
    return bool(is_sign_in_route(url) or SERVICE.search(label)
                or label in {'网上办事', '网上办事大厅', '电子邮箱', '校长信箱', '书记信箱', '书记信箱/校长信箱'}
                or re.fullmatch(r'oauth[-_]?(?:redirec|redirect|callback)', segment, re.I))


def route_rank(page, incoming=(), parents=None):
    """Prefer observed publishing leads and nearby renewal evidence."""
    parents = parents or {}
    kind = page.get('kind')
    lane = {'root': 0, 'channel': 1, 'gateway': 2, 'directory': 4}.get(kind, 5)
    from backend.services.student_sources import student_priority
    student = student_priority(kind, page.get('label', ''), page.get('path_json', '[]'))
    if kind not in ('root', 'channel', 'gateway') and student in (2, 3, 4):
        lane = min(lane, 3)
    if kind == 'navigation' and PUBLISHING.search(page.get('label', '')):
        lane = min(lane, 3)
    for edge in incoming:
        if edge.get('decision') not in ('follow', 'official_external_link'):
            continue
        parent = parents.get(edge.get('parent_url')) or {}
        if parent.get('state') != 'fetched' or parent.get('content_hash') != edge.get('content_hash'):
            continue
        feed = _feed(parent.get('feed_json'))
        if any(column.get('column_url') == page.get('url') and column.get('samples')
               for column in feed.get('lists') or [feed]):
            lane = min(lane, 1)
        same_host = urlsplit(page.get('url', '')).hostname == urlsplit(
            parent.get('final_url') or parent.get('url', '')).hostname
        if same_host and kind in ('navigation', 'gateway') and old_publications(feed):
            lane = min(lane, 2)
    return (lane, page.get('priority', 5), student if student is not None else 20,
            page.get('depth', 0), page.get('url', ''))


def choose_route(pages, *, edges=(), parents=None, claim_count=0, interval=5,
                 branches=None, history=None, slice_size=3, attention=None,
                 receipts=(), foraging_policy=None, explain=None, costs=None):
    """Read the observed layout, then share exploration across forward branches.

    Branches come from navigation_progress's current evidenced graph, not a
    second interpretation of names or address prefixes. Their attempt totals
    are scheduling effort only; they establish no departmental ownership.
    """
    if not pages:
        return None
    def selected(page, reason, demand=None, feedback=None):
        if explain is not None:
            explain({'version': 'attention-1', 'reason': reason,
                     'candidate_count': len(pages), 'claim_number': claim_count + 1,
                     'url': page['url'], 'label': page.get('label', ''),
                     'attention': demand or {}, 'foraging': feedback or {}})
        return page
    roots = [page for page in pages if page.get('kind') == 'root']
    if roots:
        return selected(min(roots, key=route_rank), 'school_root')
    parents = parents or {}
    incoming = defaultdict(list)
    for edge in edges:
        incoming[edge['target_url']].append(edge)
    from .directory_terms import academic_collection_label
    def layout_hint(record):
        return (record.get('kind') == 'directory' or record.get('kind') in ('unit', 'navigation', 'gateway')
                and academic_collection_label(record.get('label')))
    layout = [page for page in pages
              if layout_hint(page) and page.get('depth') == 1
              and not page.get('content_hash')
              and (not page.get('checked_at') or page.get('network_failures', 0) > 0
                   or page.get('health') == 'unreachable' and page.get('error'))
              and any(edge.get('decision') in ('follow', 'official_external_link')
                      and (edge.get('kind') == 'directory' or academic_collection_label(page.get('label')))
                      and (parent := parents.get(edge.get('parent_url'), {})).get('kind') == 'root'
                      and parent.get('state') == 'fetched'
                      and parent.get('content_hash')
                      and parent['content_hash'] == edge.get('content_hash')
                      for edge in incoming[page['url']])]
    if layout:
        # A failed transport attempt has not observed the school's layout.
        # Keep that priority for due retries until usable material is obtained.
        # Inventory supplies only eligible pages; its retry delays, three actual
        # failures and low-frequency recovery remain authoritative. Previously
        # read and nested directories do not regain this initial preference.
        from backend.services.student_sources import college_priority
        return selected(min(layout, key=lambda page: (
            not (academic_collection_label(page.get('label')) or
                 college_priority(page['kind'], page.get('label', ''), page.get('path_json', '[]')) == 0),
            route_rank(page))), 'observed_homepage_layout')
    if (claim_count + 1) % max(1, int(interval)) == 0:
        return selected(min(pages, key=lambda page: (
            page.get('frontier_rowid', 0), page.get('url', ''))), 'oldest_pending_turn')
    demand, feedback = {}, None
    if attention is not None:
        from .attention import candidate_attention, coverage_state
        from .foraging import ForagingFeedback
        known = history or {page['url']: page for page in pages}
        coverage = coverage_state(known, branches=branches, validated_sources=receipts, edges=edges)
        demand = {page['url']: candidate_attention(page,
            profiles=attention.get('profiles'), weights=attention.get('weights'), coverage=coverage,
            incoming=incoming[page['url']], parents=parents, branches=branches) for page in pages}
        feedback = ForagingFeedback(known, edges, receipts, branches, costs=costs,
            **(foraging_policy or {}))

    def rank(page):
        base = route_rank(page, incoming[page['url']], parents)
        if not demand:
            return base
        # Audience needs and their evidenced coverage precede local route
        # productivity. A successful busy branch cannot override that tier.
        return (*demand[page['url']]['rank'], *feedback.rank(page),
                *feedback.costs.rank(page['url']), *base)

    def explain_selected(page, reason):
        return selected(page, reason, demand.get(page['url']),
                        feedback.features(page) if feedback is not None else None)
    if branches and history and (claim_count + 1) % max(1, int(slice_size)) == 0:
        effort = defaultdict(int)
        for url, page in history.items():
            effort[branches.get(url, url)] += page.get('attempts', 0)
        probes = [page for page in pages
                  if history.get(branches.get(page['url']), {}).get('kind') == 'unit']
        if probes:
            return explain_selected(min(probes, key=lambda page: (
                effort[branches[page['url']]],
                rank(page))), 'unit_scope_rotation')
    return explain_selected(min(pages, key=rank),
                            'audience_coverage' if demand else 'observed_route')
