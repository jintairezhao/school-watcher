"""Reuse an exactly observed publishing widget in school navigation chrome.

This deduplicates discovery observations, never channels or publications. A
different main region, host, link, item or DOM structure remains independent.
"""
import hashlib
import json
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag
from .structure import locator
from backend.services.source_inventory import canonical_url, resolve_page_link


def _signature(node, url):
    def measured(value):
        if not isinstance(value, Tag):
            # Dynamic configuration is evidence too. Preserve its literal
            # strings; normalizing whitespace inside code could equate two
            # different column configurations. Nothing here executes code.
            return str(value) if value.parent.name in ('script', 'style') else str(value).strip()
        attrs = {}
        for key, item in value.attrs.items():
            if key == 'href':
                try:
                    item = resolve_page_link(url, item) or item
                except ValueError:
                    pass
            attrs[key] = item
        return [value.name, attrs, [part for child in value.children
                                  if (part := measured(child)) not in ('', None)]]
    return hashlib.sha256(json.dumps(measured(node), ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _chrome(soup, selector):
    # Without a unique observed main region there is insufficient evidence to
    # distinguish shared chrome from this page's own publishing area.
    mains = soup.find_all('main')
    if len(mains) != 1 or not selector:
        return None
    try:
        nodes = soup.select(selector)
    except (ValueError, TypeError):
        return None
    if len(nodes) != 1:
        return None
    node = nodes[0]
    if (node.name in ('html', 'body', 'head') or node is mains[0]
            or any(parent is mains[0] for parent in node.parents)
            or any(child is mains[0] for child in node.descendants)):
        return None
    return node


def repeated_navigation_observations(url, html, candidates, *, reference_url,
                                     reference_html, reference_candidates):
    """Return proof of exact chrome reuse; leave every uncertain region alone."""
    if (canonical_url(url) == canonical_url(reference_url)
            or urlsplit(url).hostname != urlsplit(reference_url).hostname):
        return []
    original = BeautifulSoup(reference_html, 'lxml')
    observed = {}
    for candidate in reference_candidates:
        node = _chrome(original, candidate.get('container_locator'))
        if node is not None:
            observed.setdefault(_signature(node, reference_url), locator(node))
    if not observed:
        return []
    page = BeautifulSoup(html, 'lxml')
    result = []
    for candidate in candidates:
        node = _chrome(page, candidate.get('container_locator'))
        if node is None:
            continue
        signature = _signature(node, url)
        if signature in observed:
            result.append({'container_locator': candidate['container_locator'],
                'reference_url': reference_url, 'reference_container_locator': observed[signature],
                'observation_hash': signature, 'basis': 'identical_observed_chrome'})
    return result
