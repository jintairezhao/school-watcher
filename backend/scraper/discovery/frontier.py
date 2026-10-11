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


def choose_route(pages, *, edges=(), parents=None, claim_count=0, interval=5):
    """Every trial interval give oldest runnable work a turn; retain every row."""
    if not pages:
        return None
    roots = [page for page in pages if page.get('kind') == 'root']
    if roots:
        return min(roots, key=route_rank)
    if (claim_count + 1) % max(1, int(interval)) == 0:
        return min(pages, key=lambda page: (page.get('frontier_rowid', 0), page.get('url', '')))
    incoming = defaultdict(list)
    for edge in edges:
        incoming[edge['target_url']].append(edge)
    return min(pages, key=lambda page: route_rank(page, incoming[page['url']], parents))
