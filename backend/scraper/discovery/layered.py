"""Follow institutional skeletons, then publishing routes; never crawl article links.

Rules establish observed routes. Ambiguous navigation gets a separate, bounded AI
turn. Relevance never suppresses an institution or erases a mixed/unknown source.
"""
import hashlib
import json

from bs4 import BeautifulSoup

from backend.services.source_inventory import canonical_url

POLICY = 'layered-1'


def prepare(inventory, key):
    """Upgrade the scratch frontier once, preserving installed sources and evidence."""
    with inventory.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS discovery_policy (site_key TEXT PRIMARY KEY, version TEXT)')
        old = c.execute('SELECT version FROM discovery_policy WHERE site_key=?', (key,)).fetchone()
        if old and old[0] == POLICY:
            return
        # Revisit skeleton pages through the new route policy. A legacy generic
        # frontier is evidence only until its parent supplies a publishing route.
        c.execute("UPDATE pages SET state='reference_only' WHERE site_key=? AND kind IN ('major','navigation') AND state!='running'", (key,))
        c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND kind IN ('root','directory','unit') AND state!='running'", (key,))
        c.execute('INSERT OR REPLACE INTO discovery_policy VALUES (?,?)', (key, POLICY))


def route_structure(parsed, page, html='', root_url='', has_publications=False):
    """Restrict expansion by page role, not department names or student keywords."""
    terminal = page['kind'] == 'channel' and has_publications
    if html and not terminal:
        from .structure import locator, document_reference, ARTICLE, SKIP
        from backend.services.source_inventory import resolve_page_link
        from backend.scraper.http_client import same_school_url
        soup = BeautifulSoup(html, 'lxml')
        known = {link.get('url') for link in parsed['links']}
        selector = 'nav a[href], [role=navigation] a[href], header a[href], h2 a[href], h3 a[href]'
        if page['kind'] == 'directory':
            selector += ', main a[href], article a[href]'
        for node in soup.select(selector):
            label = node.get_text(' ', strip=True)
            try:
                url = resolve_page_link(page['url'], node.get('href'))
            except ValueError:
                continue
            if (not url or url in known or label in SKIP or not 2 <= len(label) <= 40
                    or not same_school_url(url, root_url or page['url'])
                    or document_reference(url, label) or ARTICLE.search(url)):
                continue
            known.add(url)
            parsed['links'].append({'url': url, 'label': label, 'kind': 'navigation',
                'path': json.loads(page['path_json']), 'locator': locator(node), 'decision': 'navigation_pending'})
    unverified = {n.get('locator') for n in parsed['nodes'] if n['relation'] == 'linked_navigation_unverified'}
    for link in parsed['links']:
        if link['decision'] not in ('follow', 'official_external_link'):
            continue
        if terminal and not link.get('publication_route'):
            # More/index links discovered from a publication block may still be
            # useful, but the column's site-wide navigation is not a new seed.
            link['decision'] = 'route_reference'
        elif link['kind'] in ('navigation', 'major'):
            link['decision'] = 'navigation_pending' if link['kind'] == 'navigation' and link.get('locator') not in unverified else 'route_reference'
    return parsed


def navigation_candidates(html, parsed, page):
    """Only uncertain observed navigation; never submit whole-page DOM to AI."""
    soup = BeautifulSoup(html, 'lxml')
    seen, result = set(), []
    for link in parsed['links']:
        if link['decision'] != 'navigation_pending' or not link.get('url'):
            continue
        address = canonical_url(link['url'])
        if address in seen or address == canonical_url(page['url']):
            continue
        seen.add(address)
        node = soup.select_one(link['locator']) if link.get('locator') else None
        # Footer utility navigation doesn't extend a school's skeleton.
        if node and node.find_parent('footer'):
            continue
        context = node.parent.get_text(' ', strip=True)[:450] if node else link['label']
        result.append((link, context))
    return result


def assist(site, page, html, parsed):
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.ai.skill_loader import digest, load_skill, canonical
    from backend.services import tasks
    from backend.services.onboarding_progress import record_progress
    handle = tasks.current_execution() or {}
    try:
        binding = get_model_binding('directory')
    except AIConfigError as exc:
        return {'status': 'needs_recovery', 'error_code': exc.code}
    candidates = navigation_candidates(html, parsed, page)
    checkpoint = dict(handle.get('checkpoint') or {})
    material_hash = digest([(l['url'], l['label'], context) for l, context in candidates])
    if checkpoint.get('route_material_hash') != material_hash:
        checkpoint.update(route_material_hash=material_hash, route_cursor=0, admitted_routes=[], unresolved_routes=[])
    cursor = checkpoint.get('route_cursor', 0)
    for saved in checkpoint.get('admitted_routes', []):
        for link in parsed['links']:
            if link['url'] == saved['url'] and link['decision'] == 'navigation_pending':
                link.update(kind=saved['kind'], decision='follow')
    batch = candidates[cursor:cursor + 8]
    if not batch:
        return {'status': 'needs_recovery' if checkpoint.get('unresolved_routes') else 'processed',
                'unresolved_routes': checkpoint.get('unresolved_routes', [])}
    evidence = {'school_id': handle.get('payload', {}).get('school_id', 0), 'candidates': [], 'evidence': []}
    mapping = {}
    for link, context in batch:
        ident = hashlib.sha256(link['url'].encode()).hexdigest()[:20]
        mapping[ident] = link
        evidence['candidates'].append({'candidate_id': ident, 'name': link['label'][:600],
            'url': link['url'][:3000], 'kind': page['kind'], 'path': ' / '.join(link['path'])[-600:]})
        evidence['evidence'].append({'candidate_id': ident, 'evidence_id': ident,
            'text': link['label'] + '\n' + context})
        if len(canonical(evidence).encode()) > 24 * 1024:
            evidence['candidates'].pop(); evidence['evidence'].pop(); mapping.pop(ident)
            break
    batch_count = len(mapping)
    record_progress(ai_state='running')
    try:
        fingerprint = digest({'policy': POLICY, 'skill': load_skill('student-information', 'navigation').resource_digest,
            'evidence': evidence, 'binding': [binding['id'], binding['version']]})
        response = run_skill('student-information', 'navigation', evidence, 'directory', 'routes:' + fingerprint, binding=binding)
    except AIConfigError as exc:
        if exc.code == 'concurrency_limit':
            return {'status': 'pending', 'next_delay': 10}
        record_progress(ai_state='failed', ai_error_code=exc.code)
        return {'status': 'needs_recovery', 'error_code': exc.code}
    if response['status'] == 'pending':
        return {'status': 'pending', 'next_delay': 10}
    if response['status'] != 'succeeded':
        record_progress(ai_state='failed', ai_error_code=response.get('error_code', ''))
        return {'status': 'needs_recovery', 'error_code': response.get('error_code')}
    admitted = list(checkpoint.get('admitted_routes', []))
    unresolved = list(checkpoint.get('unresolved_routes', []))
    for row in response['output']['results']:
        link = mapping[row['candidate_id']]
        # Value affects interpretation, never a department's admission. Even
        # low-value mixed publishing sources remain inspectable/subscribable.
        kind = {'directory': 'directory', 'unit': 'unit', 'publishing': 'channel', 'gateway': 'gateway'}.get(row['role'])
        if kind:
            link.update(kind=kind, decision='follow')
            admitted.append({'url': link['url'], 'kind': kind})
        elif row['role'] == 'unknown':
            unresolved.append({'name': link['label'], 'url': link['url']})
        else:
            link['decision'] = 'route_reference'
    if handle:
        # The caller persists admitted links before deferring; replaying this
        # slice is harmless and the stable AI execution prevents double billing.
        checkpoint.update(route_cursor=cursor + batch_count, admitted_routes=admitted, unresolved_routes=unresolved)
        tasks.checkpoint(checkpoint)
    record_progress(ai_state='succeeded')
    return {'status': 'pending' if cursor + batch_count < len(candidates) else 'needs_recovery' if unresolved else 'processed',
            'unresolved_routes': unresolved}
