"""Observe school-level publishers from pinned material and current receipts.

The supplied material reader is local and read-only. This creates no Department,
does not fetch a URL, and cannot grant publisher identity from a receipt alone.
"""
import hashlib
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from backend.services.source_inventory import canonical_url, resolve_page_link


def school_publication_nodes(home, column, *, name, root_url, column_url):
    """The same original-DOM identity contract for admission and projection.

    A longer college/association name containing the school name is not a
    school publisher. A base element needs a separate, base-aware contract.
    """
    from backend.services.source_governance import _normal
    root, address = canonical_url(root_url), canonical_url(column_url)
    if (not name or not root or not address or urlsplit(root).netloc != urlsplit(address).netloc
            or home.select('base[href]') or column.select('base[href]')):
        return None

    def identity(soup):
        return next((node for node in soup.select('title,h1,header,footer,img[alt]')
            if _normal(node.get('alt') if node.name == 'img' else node.get_text(' ', strip=True))
            == _normal(name)), None)

    home_identity, column_identity = identity(home), identity(column)
    entry = next((link for link in home.select('a[href]')
        if resolve_page_link(root, link['href']) == address), None)
    if home_identity is None or column_identity is None or entry is None:
        return None
    return dict(homepage_identity=home_identity, homepage_entry=entry, column_identity=column_identity)


def observe_school_publishers(*, site, pages, departments, receipts, material):
    from backend.services.source_governance import _hash, _column_scope, FIELDS
    from backend.scraper.discovery.structure import locator
    school_id, name = site.get('id'), site.get('name')
    root = canonical_url(site.get('root_url') or '')
    if not school_id or not name or not root:
        return []
    indexed = {canonical_url(p['url']): p for p in pages}
    current = {r['source_id']: r for r in receipts}
    bodies = {}

    def document(url):
        if url not in bodies:
            page = indexed.get(url) or {}
            if (page.get('state') != 'fetched' or not page.get('checked_at')
                    or not page.get('content_hash')):
                bodies[url] = None
                return None
            try:
                html = material(url) or ''
            except (OSError, ValueError):
                html = ''
            if (page.get('state') != 'fetched' or not page.get('checked_at') or not html
                    or hashlib.sha256(html.encode()).hexdigest() != page.get('content_hash')):
                bodies[url] = None
            else:
                soup = BeautifulSoup(html, 'lxml')
                # A base-aware assertion is outside this observation contract.
                bodies[url] = None if soup.select('base[href]') else (html, soup, page)
        return bodies[url]

    def evidence(url, node, page):
        return dict(reference_url=url, locator=locator(node), content_hash=page['content_hash'],
                    observed_at=page['checked_at'], parser_revision=page.get('parser_revision'))

    result = []
    for source in departments:
        config = {field: source.get(field) or '' for field in FIELDS}
        receipt = current.get(source['id'])
        address = canonical_url(config['list_url'])
        if (source.get('school_id') != school_id or source.get('kind', 'column') != 'column'
                or config['group_name'] not in ('', name) or not receipt or not receipt.get('receipt_id')
                or receipt.get('config_hash') != _hash(config) or receipt.get('url') != address
                or not address or urlsplit(address).netloc != urlsplit(root).netloc):
            continue
        home, column = document(root), document(address)
        if not home or not column:
            continue
        nodes = school_publication_nodes(home[1], column[1], name=name, root_url=root, column_url=address)
        if nodes is None:
            continue
        records, errors, region = _column_scope(config, {'url': address}, html=column[0])
        if errors or not records or not region.get('name'):
            continue
        result.append(dict(school_id=school_id, school_name=name, root_url=root,
            source_id=source['id'], config_hash=receipt['config_hash'], receipt_id=receipt['receipt_id'],
            assertion=dict(kind='school_website_publication', predicate='publication_owner',
                direction='publisher_to_source', publisher_name=name, entry_url=address),
            evidence=dict(homepage_identity=evidence(root, nodes['homepage_identity'], home[2]),
                homepage_entry=evidence(root, nodes['homepage_entry'], home[2]),
                column_identity=evidence(address, nodes['column_identity'], column[2]))))
    return result
