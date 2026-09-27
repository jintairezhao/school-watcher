"""Evidence for university units hosted outside the university's primary domain.

An ordinary mention or friendly link is not a site's identity. These records
support candidate discovery only; independent source review is still required.
"""
import json
import re
import unicodedata

from bs4 import BeautifulSoup

from backend.scraper.http_client import official_domain, same_school_url

PREFIX = 'external_identity_evidence:'
BRANDING_PREFIX = 'page_branding_evidence:'


def branding_candidates(soup):
    """Collect explicit site identity, never general body mentions or footlinks."""
    from backend.scraper.discovery.structure import locator
    candidates = []
    if soup.title:
        candidates.append({'source': 'title', 'identity': soup.title.get_text(' ', strip=True),
                           'locator': locator(soup.title)})
    for tag in soup.select('meta[property="og:site_name"]'):
        if tag.get('content', '').strip():
            candidates.append({'source': 'og:site_name', 'identity': tag['content'].strip(),
                               'locator': locator(tag)})
    for tag in soup.select('header img[alt], .logo img[alt], #logo img[alt]'):
        if tag.get('alt', '').strip():
            candidates.append({'source': 'header_logo_alt', 'identity': tag['alt'].strip(),
                               'locator': locator(tag)})
    return candidates


def normalized_name(value):
    return re.sub(r'[\s（）()·|_-]+', '', unicodedata.normalize('NFKC', value or ''))


def website_identity_forms(value):
    """Exact branding, allowing only explicit homepage prefixes/repeated titles.

    Do not use substring matching: a news headline mentioning a college is not
    that college's website identity.
    """
    value = unicodedata.normalize('NFKC', value or '').strip()
    forms = {normalized_name(value)}
    home = re.sub(r'^(?:首页|主页|Home)\s*[-—–_|·:]\s*', '', value, flags=re.I)
    forms.add(normalized_name(home))
    parts = [normalized_name(p) for p in re.split(r'\s*[|｜]\s*', home) if p.strip()]
    if parts and len(set(parts)) == 1:
        forms.add(parts[0])
    return forms


def identity_evidence(html, site, page, final_url, content_hash):
    if page['kind'] not in ('unit', 'root'):
        return None
    school = normalized_name(site['name'])
    unit = normalized_name(re.sub(r'[（(]共建[）)]$', '', page['label']))
    identity = unit if unit.startswith(school) else school + unit
    if not school or not unit:
        return None
    soup = BeautifulSoup(html, 'lxml')
    # Exact site identity prevents "某大学代表团来访" in a news title from
    # authorizing a partner's entire website as part of that university.
    expected = {identity + suffix for suffix in ('', '首页', '官网', '官方网站', '网站', '门户网站')}
    candidates = []
    if soup.title:
        candidates.append(('title', soup.title.get_text(' ', strip=True)))
    for tag in soup.select('meta[property="og:site_name"]'):
        candidates.append(('meta[property="og:site_name"]', tag.get('content', '')))
    for tag in soup.select('header img[alt], .logo img[alt], #logo img[alt]'):
        candidates.append(('header/logo img[alt]', tag.get('alt', '')))
    for locator, label in candidates:
        if normalized_name(label) in expected:
            return {'site_key': site['site_key'], 'reference_url': page['url'],
                    'final_url': final_url, 'content_hash': content_hash,
                    'domain': official_domain(final_url), 'locator': locator, 'identity': label}
    return None


def domain_evidence(pages, site_key, target):
    """Use only current, fetched branding evidence, including its snapshot hash."""
    for page in pages:
        if page['state'] != 'fetched':
            continue
        for note in json.loads(page.get('notes_json') or '[]'):
            if not isinstance(note, str) or not note.startswith(PREFIX):
                continue
            try:
                evidence = json.loads(note[len(PREFIX):])
                reference = page.get('final_url') or page['url']
                if (evidence['site_key'] == site_key and evidence['reference_url'] == page['url']
                        and evidence['content_hash'] == page.get('content_hash')
                        and evidence['domain'] == official_domain(reference)
                        and same_school_url(target, reference)):
                    return evidence
            except (ValueError, TypeError, KeyError):
                continue
    return None


def saved_domain_evidence(inventory, site_key, target):
    with inventory.connect() as connection:
        pages = [dict(row) for row in connection.execute(
            "SELECT * FROM pages WHERE site_key=? AND state='fetched' AND notes_json LIKE ?",
            (site_key, '%' + PREFIX + '%'))]
    return domain_evidence(pages, site_key, target)
