"""Connect an official roster to observed unit sites, preserving all source IDs."""
import json
from collections import defaultdict

from backend.services.source_inventory import canonical_url, site_key
from backend.services.source_ownership import normalized_name, website_identity_forms
from backend.services.source_placements import _same_page

NAVIGATION_PREFIX = 'site_navigation_evidence:'

def navigation_evidence(soup, page_url):
    """Keep primary navigation regardless of its category/keyword vocabulary."""
    import re
    from bs4 import Tag
    from backend.scraper.discovery.structure import locator, document_reference, SERVICE
    from backend.services.source_inventory import resolve_page_link
    from backend.scraper.article_urls import ARTICLE
    result = []
    for anchor in soup.select('a[href]'):
        parents = [p for p in anchor.parents if isinstance(p, Tag)]
        if any(p.name == 'footer' or re.search(r'footer|foot', ' '.join(p.get('class', [])), re.I) for p in parents):
            continue
        if not any(p.name in ('nav', 'header') or re.search(r'nav|menu|header',
                   ' '.join(p.get('class', []))+' '+p.get('id', ''), re.I) for p in parents):
            continue
        name = anchor.get_text(' ', strip=True) or anchor.get('title') or anchor.get('aria-label') or ''
        if not name or len(name) > 120 or SERVICE.search(name):
            continue
        try:
            address = resolve_page_link(page_url, anchor.get('href'))
        except ValueError:
            continue
        if not address or document_reference(address, name) or ARTICLE.search(address):
            continue
        result.append({'name':name, 'url':address, 'locator':locator(anchor)})
    return result

def _navigations(page, key):
    try:
        notes=json.loads(page.get('notes_json') or '[]')
    except (TypeError,ValueError):
        return []
    if not isinstance(notes,list):
        return []
    for note in notes:
        if not isinstance(note, str) or not note.startswith(NAVIGATION_PREFIX):
            continue
        try:
            evidence = json.loads(note[len(NAVIGATION_PREFIX):])
            if not isinstance(evidence,dict):
                continue
            if (evidence.get('site_key') == key and evidence.get('reference_url') == page['url']
                    and evidence.get('content_hash') == page.get('content_hash')
                    and evidence.get('final_url') == (page.get('final_url') or page['url'])):
                return [link for link in evidence.get('links', []) if isinstance(link,dict)
                        and link.get('url') and link.get('locator')]
        except (TypeError, ValueError):
            continue
    return []

def directory_websites(school, units, entries, pages):
    """No name-only join: roster + scoped parent nav + exact target branding.

    This establishes website placement, not an administrative relation inferred
    from a menu. Parentage is supplied by the actual official roster entries.
    A same-named centre linked by two parents stays unresolved automatically.
    """
    from backend.services.source_channels import _page_identity
    from backend.scraper.http_client import same_school_url
    key = site_key(school.url)
    indexed = {unit.id:unit for unit in units if unit.kind == 'unit' and unit.school_id == school.id}
    parents = defaultdict(set)
    for entry in entries:
        if entry.parent_id in indexed and entry.department_id in indexed:
            parents[entry.department_id].add(entry.parent_id)
    readable = [page for page in pages if page.get('state') == 'fetched'
                and page.get('content_hash') and 'unit_profile_page:' not in (page.get('notes_json') or '')
                and same_school_url(page.get('final_url') or page['url'], school.url)]
    result = {}
    for ident, owner_ids in parents.items():
        unit = indexed[ident]
        if unit.list_url:
            continue
        expected = {normalized_name(unit.name), normalized_name(school.name+unit.name)}
        sites = {}
        for page in readable:
            identity = _page_identity(page, school, '')
            if not identity or not website_identity_forms(identity) & expected:
                continue
            target = page.get('final_url') or page['url']
            # A matched article/header is not a whole unit website.
            from backend.scraper.article_urls import ARTICLE
            if ARTICLE.search(target):
                continue
            matches = []
            for parent_id in owner_ids:
                parent = indexed[parent_id]
                if not parent.list_url:
                    continue
                for parent_page in readable:
                    if not _same_page(parent_page.get('final_url') or parent_page['url'], parent.list_url):
                        continue
                    forward = [link for link in _navigations(parent_page,key) if _same_page(link['url'],target)]
                    backward = [link for link in _navigations(page,key) if _same_page(link['url'],parent.list_url)]
                    if forward and backward:
                        matches.append(parent_id)
            if len(set(matches)) == 1:
                sites[canonical_url(target)] = {'url':target,'parent_id':matches[0], 'unit_id':ident,
                    'reference_urls':[indexed[matches[0]].list_url,page['url']],
                    'content_hash':page['content_hash']}
        if len(sites) == 1:
            result[ident] = next(iter(sites.values()))
    return result

def navigation_parent(source,school,units,pages):
    """Locate a legacy column in a unit's actual primary navigation.

    This retains a website browsing path; it never invents a subordinate unit.
    A legacy prefix only narrows candidates. A current, identified parent's
    primary navigation proves a browsing path without requiring a backlink.
    This does not fill a roster child's missing administrative relationship.
    """
    from backend.services.source_channels import _page_identity, _scope_contains
    key=site_key(school.url)
    pages=[p for p in pages if p.get('state')=='fetched' and p.get('content_hash')]
    candidates={}
    for unit in units:
        if unit.school_id!=school.id or unit.kind!='unit' or not unit.list_url or not any(
                source.name.startswith(unit.name+sep) for sep in ('-','－','—')):
            continue
        entrances=[p for p in pages if _same_page(p.get('final_url') or p['url'],unit.list_url)]
        expected={normalized_name(unit.name),normalized_name(school.name+unit.name)}
        forward=[link for page in entrances
                 if website_identity_forms(_page_identity(page,school,'')) & expected
                 for link in _navigations(page,key) if _scope_contains(source.list_url,link['url'])
                 and not _same_page(link['url'],school.url) and not _same_page(link['url'],unit.list_url)]
        if forward:
            from urllib.parse import urlsplit
            closest=max(len(urlsplit(link['url']).path) for link in forward)
            forward=[link for link in forward if len(urlsplit(link['url']).path)==closest]
            labels={link['name'] for link in forward}
            label=next(iter(labels)) if len(labels)==1 else ''
            names=set()
            for page in pages:
                if not _same_page(page.get('final_url') or page['url'],source.list_url):
                    continue
                try:
                    feeds=json.loads(page.get('feed_json') or '{}').get('lists',[])
                except (TypeError,ValueError,AttributeError):
                    continue
                feeds=page.get('column_regions',feeds)
                names.update(feed['name'] for feed in feeds if isinstance(feed,dict) and feed.get('name')
                             and feed.get('heading_locator') and feed.get('container_locator')
                             and _same_page(feed.get('column_url'),source.list_url))
            if label and len(names)==1 and label not in names:
                label += ' / '+next(iter(names))
            candidates[unit.id]={'unit_id':unit.id,'name':label}
            continue
        from backend.scraper.article_urls import ARTICLE
        targets=[p for p in pages if 'unit_profile_page:' not in (p.get('notes_json') or '')
                 and not ARTICLE.search(p.get('final_url') or p['url']) and
                 (_same_page(p.get('final_url') or p['url'],source.list_url)
                  or _scope_contains(source.list_url,p.get('final_url') or p['url']))]
        for target in targets:
            back=[link for link in _navigations(target,key) if _same_page(link['url'],unit.list_url)]
            if not back:
                continue
            identity=website_identity_forms(_page_identity(target,school,''))
            if identity & expected:
                candidates[unit.id]={'unit_id':unit.id,'name':''}
    return next(iter(candidates.values())) if len(candidates)==1 else None
