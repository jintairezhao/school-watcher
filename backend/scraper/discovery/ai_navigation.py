"""Use AI on the first official pages; only observed, same-school links can be queued."""
import hashlib
import json

from bs4 import BeautifulSoup


def assist_navigation(site, page, html, parsed):
    from backend.services import tasks
    from backend.services.onboarding_progress import record_progress
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.scraper.http_client import same_school_url
    from backend.services.source_inventory import resolve_page_link
    handle = tasks.current_execution()
    if not handle or handle.get('kind') not in ('discover', 'directory') or page['kind'] not in ('root', 'directory'):
        return
    if page['depth'] > 1 or handle.get('payload', {}).get('ai_assist') is False:
        return
    try:
        binding = get_model_binding('directory')
    except AIConfigError:
        record_progress(ai_state='not_configured')
        return
    # Budget a bounded set of first-level pages for each discovery generation.
    completed = dict(handle.get('checkpoint', {}).get('ai_navigation') or {})
    page_key = hashlib.sha256(page['url'].encode()).hexdigest()
    if page_key in completed or len(completed) >= 4:
        return
    soup = BeautifulSoup(html, 'lxml')
    for tag in soup.select('script,style,iframe'):
        tag.decompose()
    known = {link['url']: link for link in parsed['links']}
    anchors = []
    seen = set()
    for anchor in soup.select('a[href]'):
        url = resolve_page_link(page['url'], anchor.get('href', ''))
        name = anchor.get_text(' ', strip=True)[:200]
        if not url or not name or url in seen or not same_school_url(url, site['root_url']):
            continue
        seen.add(url)
        anchors.append({'candidate_id': 'link-' + str(len(anchors)), 'name': name, 'url': url,
                        'kind_hint': known.get(url, {}).get('kind', 'unknown')})
    anchors.sort(key=lambda a: a['kind_hint'] not in ('directory', 'unit', 'channel'))
    anchors = anchors[:32]
    if not anchors:
        return
    evidence = {'school_id': handle.get('payload', {}).get('school_id', site['site_key']),
        'candidates': anchors, 'entities': [{'id': 'school', 'name': site['name']}],
        'observed_urls': [a['url'] for a in anchors],
        'evidence': [{'evidence_id': 'page', 'url': page['url'], 'html': str(soup)[:100000]}]}
    record_progress(phase='ai', ai_state='running', current_label=page['label'] or site['name'])
    execution = f"navigation:{handle['id']}:{handle.get('generation', 1)}:{page_key[:20]}"
    try:
        result = run_skill('university-source-onboarding', 'classify', evidence, 'directory', execution,
                           expected_version=binding['version'], binding=binding)
    except Exception:
        # Discovery remains usable if a supplier/configuration becomes unavailable.
        result = {'status': 'failed'}
    completed[page_key] = result.get('status', 'failed')
    tasks.checkpoint(dict(handle.get('checkpoint') or {}, ai_navigation=completed))
    record_progress(phase='crawl', ai_state=result.get('status', 'failed'))
    if result.get('status') != 'succeeded':
        return
    candidates = {a['candidate_id']: a for a in anchors}
    for row in (result.get('output') or {}).get('results', []):
        anchor = candidates.get(row.get('candidate_id'))
        if not anchor or row.get('decision') != 'propose' or row.get('kind') not in ('directory', 'unit', 'channel'):
            continue
        # Classification only extends the crawl frontier; it never activates sources
        # or asserts institutional ownership without the independent evidence gate.
        if anchor['url'] not in known:
            parsed['links'].append({'url': anchor['url'], 'label': anchor['name'], 'kind': row['kind'],
                'path': json.loads(page['path_json']), 'locator': 'ai-observed-link', 'decision': 'follow'})
