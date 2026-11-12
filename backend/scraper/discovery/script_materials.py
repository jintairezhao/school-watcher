"""Original page text with separately captured, content-addressed script material.

This never fetches or executes a script. Callers supply acquired bytes; a script
URL or equal-sized tab collections alone cannot establish a binding.
"""
from datetime import datetime
import hashlib
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from backend.services.source_inventory import canonical_url


class ScriptHTML(str):
    def __new__(cls, text, scripts, url):
        value = super().__new__(cls, text)
        value.script_materials = tuple(scripts)
        value.final_url = url
        if getattr(text, 'result', None) is not None:
            value.result = text.result
        return value


def verified_scripts(html, url, soup=None):
    """Return only actual script elements backed by this exact document's bytes."""
    records = getattr(html, 'script_materials', ())
    if not isinstance(records, (tuple, list)) or not records or len(records) > 8:
        return []
    from .structure import locator
    soup = soup if soup is not None else BeautifulSoup(html, 'lxml')
    if soup.select('base[href]'):
        return []  # A base-aware capture contract has not been established.
    page_hash = hashlib.sha256(str(html).encode()).hexdigest()
    result = []
    for record in records:
        if not isinstance(record, dict):
            continue
        text = record.get('text')
        if (not isinstance(text, str) or not isinstance(record.get('page_url'), str)
                or not isinstance(record.get('url'), str) or len(text.encode()) > 2*1024*1024
                or record.get('status') != 200 or record.get('page_hash') != page_hash
                or canonical_url(record.get('page_url') or '') != canonical_url(url)
                or hashlib.sha256(text.encode()).hexdigest() != record.get('hash')):
            continue
        try:
            observed = datetime.fromisoformat(record.get('observed_at') or '')
            address = canonical_url(record.get('url') or '')
        except (ValueError, TypeError):
            continue
        if observed.tzinfo is None or not address:
            continue
        elements = [s for s in soup.select('script[src]')
                    if canonical_url(urljoin(url, s['src'])) == address]
        if len(elements) != 1:
            continue
        proof = dict(script_url=address, script_hash=record['hash'], script_locator=locator(elements[0]),
            page_url=canonical_url(url), page_hash=page_hash, observed_at=record['observed_at'])
        result.append((elements[0], text, proof))
    return result
