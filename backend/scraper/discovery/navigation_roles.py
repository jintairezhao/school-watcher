"""Reuse roles only for a navigation region observed again in full.

This cache never supplies a source name, owner, extraction rule or permission to
follow an unobserved address. A matching URL alone is deliberately insufficient.
"""
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import Tag

from backend.ai.skill_loader import digest
from backend.services.source_inventory import resolve_page_link, site_key

VERSION = 'observed-navigation-1'
ROLES = {'directory', 'unit', 'publishing', 'gateway'}
READ_OBSERVED = 'read_observed'


def _navigation(node):
    return isinstance(node, Tag) and node.name != 'a' and (node.name == 'nav' or node.get('role') == 'navigation'
        or bool(re.search(r'nav|menu', ' '.join(node.get('class', [])) + ' ' + node.get('id', ''), re.I)))


def footer_navigation(node):
    """Recognize semantic and template footers without using a link's name."""
    return node is not None and any(isinstance(parent, Tag) and (
        parent.name == 'footer' or parent.get('role') == 'contentinfo'
        or re.search(r'(?:^|[\s_-])(?:footer|foot)(?=$|[\s_-])',
                     ' '.join(parent.get('class', [])) + ' ' + parent.get('id', ''), re.I))
        for parent in [node, *node.parents])


def region_material(node, page_url, cache=None):
    """Keep menu hierarchy, text, resolved addresses and its distinct DOM region.

    Equivalent relative links and inert JS placeholders may differ between
    pages. Executable attributes are never included or evaluated. Distinct
    menus on one page retain their ordinal and semantic landmark identity.
    """
    if node is None:
        return None
    region = next((parent for parent in [node, *node.parents] if _navigation(parent)), None)
    if region is None or footer_navigation(region):
        return None
    cache = {} if cache is None else cache
    shared = cache.get(id(region))
    if shared:
        return _candidate_material(node, shared)
    attrs = ('id', 'class', 'role', 'title', 'aria-label', 'alt')

    def identity(tag):
        return [tag.name, {key: tag[key] for key in attrs if tag.get(key)}]

    root = region
    while isinstance(root.parent, Tag):
        root = root.parent
    peers = [tag for tag in root.find_all(True) if _navigation(tag) and identity(tag) == identity(region)]
    ordinal = next((index for index, tag in enumerate(peers) if tag is region), 0)
    landmarks = [identity(parent) for parent in region.parents if isinstance(parent, Tag)
                 and parent.name in ('header', 'main', 'article', 'aside', 'section', 'footer')]
    tokens, positions = [], {}

    def walk(tag, depth):
        if tag.name in ('script', 'style', 'svg'):
            return
        positions[id(tag)] = len(tokens)
        token = [depth, *identity(tag)]
        if tag.name == 'a':
            try:
                target = resolve_page_link(page_url, tag.get('href', ''))
            except ValueError:
                return
            token.append(target)
        tokens.append(token)
        for child in tag.children:
            if isinstance(child, Tag):
                walk(child, depth + 1)
            elif str(child).strip():
                tokens.append(['text', re.sub(r'\s+', ' ', str(child)).strip()])

    walk(region, 0)
    shared = {'key': digest([VERSION, urlsplit(page_url).netloc, identity(region), ordinal,
                            landmarks, tokens]), 'positions': positions,
              'text': region.get_text(' ', strip=True)}
    cache[id(region)] = shared
    return _candidate_material(node, shared)


def _candidate_material(node, shared):
    candidate_position = shared['positions'].get(id(node))
    if candidate_position is None:
        return None
    from .structure import menu_path
    hierarchy = [name for name, _ in menu_path(node)]
    parent_text = node.parent.get_text(' ', strip=True)
    text = '\n'.join([shared['text'], *hierarchy,
                      parent_text])
    # All material allowed as a supporting quote must participate in the key:
    # a nested submenu's real owner heading can sit outside its own region.
    return {'key': digest([shared['key'], candidate_position, hierarchy, parent_text]),
            'text': text, 'hierarchy': hierarchy,
            'quote_text': '\n'.join([*hierarchy, parent_text])[:2000]}


def candidate_key(site, link, material, binding, skill_digest, policy):
    if not material:
        return None
    return digest([VERSION, site_key(site['root_url']), policy,
                   binding['id'], binding['version'], skill_digest, material['key'],
                   link['url'], link['label'], link['kind']])


def _path():
    from flask import current_app, has_app_context
    if not has_app_context():
        return None
    value = current_app.config.get('DISCOVERY_CACHE_PATH')
    return Path(value) if value else None


def _connect():
    path = _path()
    if not path:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute('CREATE TABLE IF NOT EXISTS navigation_role_cache ('
        'cache_key TEXT PRIMARY KEY, output_json TEXT NOT NULL)')
    return connection


def supported(row, material):
    role = row.get('role')
    if (not material or not row.get('facts') or
            role not in ROLES and not (role == 'unknown' and row.get('action') == READ_OBSERVED)):
        return False
    corpus = re.sub(r'\s+', ' ', material['text']).strip()
    return all(re.sub(r'\s+', ' ', fact.get('quote', '')).strip()
               and re.sub(r'\s+', ' ', fact.get('quote', '')).strip() in corpus
               for fact in row['facts'])


def cacheable_result(row, material):
    """Cache uncertainty only as permission to read its observed target.

    The semantic role and value remain unknown. No source identity, owner,
    extraction rule or target-body classification is supplied by this action.
    """
    value = dict(row)
    if value.get('role') == 'unknown':
        value['action'] = READ_OBSERVED
    return value if supported(value, material) else None


def load(keys):
    keys = [key for key in keys if key]
    if not keys:
        return {}
    connection = _connect()
    if connection is None:
        return {}
    try:
        rows = connection.execute('SELECT cache_key,output_json FROM navigation_role_cache '
            'WHERE cache_key IN (' + ','.join('?' for _ in keys) + ')', keys).fetchall()
        connection.commit()
        return {key: json.loads(value) for key, value in rows}
    finally:
        connection.close()


def save(rows):
    rows = [(key, json.dumps(row, ensure_ascii=False, sort_keys=True)) for key, row in rows if key]
    if not rows:
        return
    connection = _connect()
    if connection is None:
        return
    try:
        connection.executemany('INSERT OR IGNORE INTO navigation_role_cache VALUES (?,?)', rows)
        connection.commit()
    finally:
        connection.close()
