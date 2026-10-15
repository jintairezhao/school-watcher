"""Explore observed publishing routes with bounded, cached AI navigation.

Student discovery prioritizes useful information; the legacy layered audit still
retains exhaustive institutional traversal. Neither invents links or ownership.
"""
import hashlib
import json
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from backend.services.source_inventory import canonical_url

POLICY = 'layered-1'
VALUE_POLICY = 'student-routes-2'
LANGUAGE_ENTRANCES = {'中文', '中文网', '中文网站', '中文版', '进入网站', '进入学院', '进入首页'}
PUBLISHING_GATEWAYS = {'学生工作', '学生事务', '本科生教育', '研究生教育', '本科生培养',
                       '研究生培养', '本科教学', '研究生教学', '人才培养', '教育教学', '教学工作', '教务信息'}


def prepare(inventory, key, policy='layered'):
    """Upgrade the scratch frontier once, preserving installed sources and evidence."""
    with inventory.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS discovery_policy (site_key TEXT PRIMARY KEY, version TEXT)')
        old = c.execute('SELECT version FROM discovery_policy WHERE site_key=?', (key,)).fetchone()
        version = VALUE_POLICY if policy == 'valuable' else POLICY
        if old and old[0] == version:
            return False
        # Revisit skeleton pages through the new route policy. A legacy generic
        # frontier is evidence only until its parent supplies a publishing route.
        c.execute("UPDATE pages SET state='reference_only' WHERE site_key=? AND kind IN ('major','navigation') AND state!='running'", (key,))
        # The student route upgrade revisits the homepage only. Existing useful
        # sources and unfinished leads survive without restarting every office.
        kinds = "('root')" if policy == 'valuable' else "('root','directory','unit')"
        c.execute(f"UPDATE pages SET state='pending' WHERE site_key=? AND kind IN {kinds} AND state!='running'", (key,))
        c.execute('INSERT OR REPLACE INTO discovery_policy VALUES (?,?)', (key, version))
        return True


def route_structure(parsed, page, html='', root_url='', has_publications=False, *, policy='layered', publication=None):
    """Restrict expansion by page role, not department names or student keywords."""
    if policy == 'valuable':
        parsed['navigation_policy'] = VALUE_POLICY
    actual_url = next((node.get('url') for node in parsed['nodes']
                       if node.get('relation') == 'page_identity' and node.get('url')),
                      page.get('final_url') or page['url'])
    soup = BeautifulSoup(html, 'lxml') if html else None
    base = soup.find('base', href=True) if soup else None
    base_url = urljoin(actual_url, base['href']) if base else actual_url
    from .frontier import article_addresses, observed_label, utility_route
    known_articles = article_addresses(publication)
    terminal = page['kind'] == 'channel' and has_publications
    if html and terminal:
        from .frontier import old_publications, local_navigation
        if publication is None:
            from .structure import publication_evidence
            publication = publication_evidence(html, actual_url)
            known_articles = article_addresses(publication)
        if old_publications(publication):
            from .structure import locator, document_reference, ARTICLE, SKIP, SERVICE
            from backend.services.source_inventory import resolve_page_link
            for node in soup.select('a[href]'):
                if not local_navigation(node):
                    continue
                label = observed_label(node)
                if label in SKIP - {'首页', '返回首页', '网站首页'} or SERVICE.search(label):
                    continue
                try:
                    target = resolve_page_link(base_url, node['href'])
                except ValueError:
                    continue
                if (not target or target in {canonical_url(page['url']), canonical_url(actual_url)}
                        or urlsplit(target).hostname != urlsplit(actual_url).hostname
                        or target in known_articles or utility_route(target, label)
                        or document_reference(target, label) or ARTICLE.search(target)):
                    continue
                position = locator(node)
                link = next((link for link in parsed['links'] if link.get('url') == target
                             and link.get('locator') == position), None)
                if link is None:
                    link = {'url': target, 'label': label, 'kind': 'navigation',
                            'path': json.loads(page['path_json']), 'locator': position}
                    parsed['links'].append(link)
                # Cheap reading is not a classification or ownership verdict.
                # AI and the original scope/body validator judge its real page.
                link.update(decision='follow', probe_only=True)
    if html and not terminal:
        from .structure import locator, document_reference, ARTICLE, SKIP, SERVICE
        from backend.services.source_inventory import resolve_page_link
        from backend.scraper.http_client import same_school_url
        known = {link.get('url') for link in parsed['links']}
        if page['kind'] in ('root', 'unit', 'gateway'):
            # Language splash pages are literal website entrances. Following a
            # same-host link needs no model and grants no new domain ownership.
            for node in soup.select('a[href]'):
                label = observed_label(node)
                if label not in LANGUAGE_ENTRANCES:
                    continue
                try:
                    target = resolve_page_link(base_url, node['href'])
                except ValueError:
                    continue
                if (not target or target in {canonical_url(page['url']), canonical_url(actual_url)}
                        or urlsplit(target).hostname != urlsplit(actual_url).hostname
                        or document_reference(target, label) or ARTICLE.search(target)):
                    continue
                from .structure import node_key
                parsed['nodes'].append({'key':node_key('group',label,target), 'name':label,
                    'kind':'group', 'url':target, 'parent':'', 'locator':locator(node),
                    'relation':'website_entrance'})
                existing = next((link for link in parsed['links'] if link.get('url') == target), None)
                if existing:
                    existing.update(kind='gateway', decision='follow')
                else:
                    parsed['links'].append({'url':target, 'label':label, 'kind':'gateway',
                        'path':json.loads(page['path_json']), 'locator':locator(node), 'decision':'follow'})
                    known.add(target)

        selector = 'nav a[href], [role=navigation] a[href], header a[href], h2 a[href], h3 a[href]'
        if policy == 'valuable':
            # Many university templates use div menus instead of semantic nav.
            # Include visible cards; article URLs, utilities and files are still
            # excluded below and the model receives only a small link batch.
            selector += ', [class*=nav] a[href], [class*=menu] a[href], [id*=nav] a[href], [id*=menu] a[href], main a[href], section a[href], [class*=card] a[href], [class*=banner] a[href], [class*=topic] a[href]'
        if page['kind'] == 'directory':
            selector += ', main a[href], article a[href]'
        for node in soup.select(selector):
            from .frontier import local_navigation
            label = observed_label(node)
            probe = (policy == 'valuable' and local_navigation(node)
                     and (node.find('img') is not None or not label))
            try:
                url = resolve_page_link(base_url, node.get('href'))
            except ValueError:
                continue
            if (not url or url in {canonical_url(page['url']), canonical_url(actual_url)}
                    or url in known or label in SKIP or SERVICE.search(label)
                    or (not probe and not 2 <= len(label) <= 40)
                    or not same_school_url(url, root_url or page['url'])
                    or url in known_articles or utility_route(url, label)
                    or document_reference(url, label) or ARTICLE.search(url)):
                continue
            known.add(url)
            parsed['links'].append({'url': url, 'label': label, 'kind': 'navigation',
                'path': json.loads(page['path_json']), 'locator': locator(node),
                'decision': 'follow' if probe else 'navigation_pending', **({'probe_only': True} if probe else {})})
    unverified = {n.get('locator') for n in parsed['nodes'] if n['relation'] == 'linked_navigation_unverified'}
    for link in parsed['links']:
        from backend.scraper.http_client import same_school_url
        if (html and not link.get('publication_route') and link.get('url')
                and not same_school_url(link['url'], root_url or page['url'])
                and link['decision'] in ('follow', 'official_external_link', 'navigation_pending', 'external_review')):
            from .structure import locator_element
            from .navigation_roles import footer_navigation
            element = locator_element(soup, link.get('locator'))
            if element is not None and element.name != 'script' and footer_navigation(element):
                # Keep the observed resource edge. A footer backlink alone is
                # not evidence that an unrelated institution belongs to this
                # school or merits another paid navigation classification.
                link['decision'] = 'auxiliary_reference'
                continue
        if (not terminal and page['kind'] in ('unit', 'gateway')
                and link['kind'] == 'navigation' and link['label'] in PUBLISHING_GATEWAYS
                and link['decision'] in ('follow', 'navigation_pending')
                and urlsplit(link['url']).hostname == urlsplit(page['url']).hostname):
            link.update(kind='gateway', decision='follow')
            for node in parsed['nodes']:
                if node.get('locator') == link.get('locator') and node['relation'] in ('navigation_entry', 'linked_navigation_unverified'):
                    node['relation'] = 'website_entrance'
        if link['decision'] not in ('follow', 'official_external_link'):
            continue
        if link.get('probe_only'):
            continue
        if terminal and not link.get('publication_route'):
            # More/index links discovered from a publication block may still be
            # useful, but the column's site-wide navigation is not a new seed.
            link['decision'] = 'route_reference'
        elif link['kind'] in ('navigation', 'major'):
            link['decision'] = 'navigation_pending' if link['kind'] == 'navigation' and link.get('locator') not in unverified else 'route_reference'
    return parsed


def navigation_candidates(html, parsed, page):
    """Review observed units and columns as well as unfamiliar entrance labels."""
    soup = BeautifulSoup(html, 'lxml')
    primary = page.get('discovery_policy') == 'valuable'
    headings = [node.get_text(' ', strip=True)[:200] for node in soup.select('title, h1')[:3]]
    seen, result, region_cache = set(), [], {}
    for link in parsed['links']:
        known_route = (primary and link['decision'] in ('follow', 'official_external_link')
                       and link['kind'] in ('directory', 'unit', 'channel', 'gateway'))
        if (link.get('ai_reviewed') or not link.get('url')
                or link['decision'] != 'navigation_pending' and not known_route):
            continue
        if link['label'] in LANGUAGE_ENTRANCES:
            continue
        address = canonical_url(link['url'])
        if address in seen or address == canonical_url(page['url']):
            continue
        seen.add(address)
        from .structure import locator_element
        node = locator_element(soup, link.get('locator'))
        from .script_directories import literal_navigation_material
        literal = literal_navigation_material(node, link, page['url'], region_cache)
        # Footer utility navigation doesn't extend a school's skeleton.
        # A literal menu may be declared in a deferred footer script while its
        # entries render in the site's main navigation.
        if node and node.find_parent('footer') and not literal:
            continue
        from .navigation_roles import region_material
        link['_navigation_material'] = literal or region_material(node, page['url'], region_cache)
        context = (literal['text'][:1100] if literal else
                   node.parent.get_text(' ', strip=True)[:1100] if node else link['label'])
        context = '\n'.join([*headings, ' / '.join(link.get('path', []))[-400:], context])
        result.append((link, context))
    return result


def apply_route(parsed, link, saved):
    """Persist only role decisions; names and relationships come from the DOM."""
    from .structure import node_key
    link.update({key: saved[key] for key in ('kind', 'decision', 'ai_reviewed', 'ai_value', 'ai_role', 'probe_only') if key in saved})
    if not saved.get('ai_reviewed') or link['decision'] not in ('follow', 'official_external_link'):
        return
    if saved.get('ai_role') == 'unknown':
        # A bounded read action is not evidence of a website entrance's owner
        # or semantic role. Keep the independently observed DOM nodes intact.
        return
    kind = {'unit': 'unit', 'channel': 'channel'}.get(link['kind'], 'group')
    nodes = [node for node in parsed['nodes'] if node.get('url') == link['url']
             and node.get('locator') == link.get('locator') and node['relation'] != 'page_identity']
    for node in nodes:
        # A parsed publication block is stronger evidence than a route hint.
        if node['relation'] == 'publication_column':
            continue
        old_key = node['key']
        node.update(kind=kind, key=node_key(kind, node['name'], node['url']))
        if link['kind'] == 'gateway':
            node['relation'] = 'website_entrance'
        for child in parsed['nodes']:
            if child.get('parent') == old_key:
                child['parent'] = node['key']
    if not nodes and link.get('locator'):
        owner = next((node for node in parsed['nodes'] if node['relation'] == 'page_identity'), None)
        parsed['nodes'].append({'key': node_key(kind, link['label'], link['url']),
            'name': link['label'], 'kind': kind, 'url': link['url'],
            'parent': owner['key'] if owner else '', 'locator': link['locator'],
            'relation': 'website_entrance' if link['kind'] == 'gateway' else 'navigation_entry'})


def route_priority(link):
    from backend.services.source_inventory import KINDS
    return KINDS.get(link['kind'], 4) + {
        'relevant': -2, 'potential': -1, 'mixed': 0, 'unknown': 0, 'low': 2}.get(link.get('ai_value'), 0)


def observed_probe(site, link):
    """Read a real uncertain entrance without promoting it to a source."""
    from backend.scraper.http_client import same_school_url
    return {'url': link['url'], 'kind': 'channel' if link.get('publication_route') else 'gateway',
            'decision': 'follow' if same_school_url(link['url'], site['root_url']) else 'official_external_link',
            'ai_role': 'unknown', 'probe_only': True}


def append_navigation_evidence(evidence, link, context, *, menu_evidence=True):
    ident = hashlib.sha256(link['url'].encode()).hexdigest()[:20]
    evidence['candidates'].append({'candidate_id': ident, 'name': link['label'][:600],
        'url': link['url'][:3000], 'kind': link['kind'], 'path': ' / '.join(link['path'])[-600:]})
    proof = {'candidate_id': ident, 'evidence_id': ident, 'text': link['label'] + '\n' + context}
    material = link.get('_navigation_material') if menu_evidence else None
    if material:
        # The validator permits facts to quote only text. Page context can
        # guide priority without contaminating a reusable menu role's proof.
        proof.update(text=(material.get('quote_text') or material['text'])[:2000], context=proof['text'])
    evidence['evidence'].append(proof)
    return ident


def legacy_navigation_decision(batch, school_id, max_bytes):
    """Recover an old unresolved full-batch request before filtering role hits."""
    from flask import has_app_context
    if not has_app_context():
        return None
    from backend.ai.models import AIExecution
    from backend.ai.skill_loader import canonical, digest
    prefixes = {}
    for menu_evidence in (True, False):
        evidence = {'school_id': school_id, 'candidates': [], 'evidence': []}
        for index, (link, context) in enumerate(batch):
            append_navigation_evidence(evidence, link, context, menu_evidence=menu_evidence)
            if len(canonical(evidence).encode()) > max_bytes:
                break
            # Recover both input formats exactly. Changing the quote boundary
            # must never purchase a new subset of an unresolved old request.
            prefixes[digest({'evidence': evidence, 'repair_feedback': None})] = (
                index + 1, json.loads(json.dumps(evidence)))
    if not prefixes:
        return None
    rows = AIExecution.query.filter(AIExecution.purpose == 'directory',
        AIExecution.skill_id == 'student-information', AIExecution.mode == 'navigation',
        AIExecution.status.in_(('reserved', 'sending', 'uncertain', 'failed', 'partial')),
        AIExecution.execution_id.like('routes:%'),
        AIExecution.input_digest.in_(prefixes)).order_by(AIExecution.created_at).all()
    row = next((row for row in rows if row.status in ('reserved', 'sending', 'uncertain')
                or row.request_sent and not (row.usage or {}).get('known')), None)
    if row is None:
        return None
    batch_count, original_evidence = prefixes[row.input_digest]
    return {'batch_count': batch_count, 'binding': [row.profile_id, row.config_version],
            'skill_digest': row.skill_digest, 'fingerprint': row.execution_id[len('routes:'):],
            'evidence': original_evidence}


def assist(site, page, html, parsed, *, decision_allowed=None):
    from backend.ai.configuration import get_model_binding, AIConfigError
    from backend.ai.runtime import run_skill
    from backend.ai.skill_loader import digest, load_skill, canonical
    from backend.services import tasks
    from backend.services.onboarding_progress import record_progress
    handle = tasks.current_execution() or {}
    if handle.get('payload', {}).get('ai_assist') is False:
        return {'status': 'skipped', 'error_code': 'ai_disabled', 'reviewed_routes': 0, 'cached_routes': 0}
    candidates = navigation_candidates(html, parsed, page)
    cached_count = len({link['url'] for link in parsed['links'] if link.get('ai_reviewed')})
    valuable = page.get('discovery_policy') == 'valuable'
    from backend.services.discovery_policy import get_policy
    policy = get_policy()
    batch_size = max(1, min(8, int(policy['navigation_batch_size'])))
    checkpoint = dict(handle.get('checkpoint') or {})
    material_hash = digest([(l['url'], l['label'], context) for l, context in candidates])
    pending_decision = checkpoint.get('route_pending_decision')
    if checkpoint.get('route_material_hash') != material_hash:
        if pending_decision:
            # An unresolved paid attempt remains identifiable even if current
            # evidence changes. Never replace it with a newly billed subset.
            return {'status': 'needs_recovery', 'error_code': 'execution_conflict',
                    'reviewed_routes': checkpoint.get('route_cursor', 0), 'cached_routes': cached_count}
        checkpoint.update(route_material_hash=material_hash, route_cursor=0, admitted_routes=[], unresolved_routes=[],
                          cached_navigation_routes=0)
    cursor = checkpoint.get('route_cursor', 0)
    if pending_decision and (pending_decision.get('material_hash') != material_hash
                             or pending_decision.get('cursor') != cursor):
        return {'status': 'needs_recovery', 'error_code': 'execution_conflict',
                'reviewed_routes': cursor, 'cached_routes': cached_count}
    for saved in checkpoint.get('admitted_routes', []):
        for link in parsed['links']:
            if link['url'] == saved['url']:
                apply_route(parsed, link, dict(saved, decision=saved.get('decision', 'follow')))
    if checkpoint.get('unresolved_routes') and not pending_decision:
        # Older layered jobs recorded a known unknown decision but no read
        # action. Advance only the already-reviewed, still-observed URLs. This
        # does not fabricate cache facts or purchase a new subset decision.
        processed = {link['url']: link for link, _ in candidates[:cursor]}
        unresolved, admitted = [], list(checkpoint.get('admitted_routes', []))
        for row in checkpoint['unresolved_routes']:
            link = processed.get(row['url'])
            if link is None:
                unresolved.append(row)
                continue
            if not link.get('ai_reviewed'):
                saved = dict(observed_probe(site, link), ai_reviewed=True, ai_value='unknown')
                apply_route(parsed, link, saved)
                admitted = [value for value in admitted if value['url'] != saved['url']]
                admitted.append(saved)
        checkpoint.update(unresolved_routes=unresolved, admitted_routes=admitted)
        if handle:
            tasks.checkpoint(checkpoint)
    batch = candidates[cursor:cursor + (pending_decision['batch_count'] if pending_decision else batch_size)]
    if not batch:
        return {'status': 'needs_recovery' if checkpoint.get('unresolved_routes') else 'processed',
                'unresolved_routes': checkpoint.get('unresolved_routes', []), 'deferred_routes': 0, 'remaining_routes': 0,
                'reviewed_routes': cursor, 'cached_routes': cached_count}
    try:
        binding = get_model_binding('directory')
    except AIConfigError as exc:
        return {'status': 'needs_recovery', 'error_code': exc.code,
                'reviewed_routes': cursor, 'cached_routes': cached_count}
    from . import navigation_roles
    skill_digest = load_skill('student-information', 'navigation').resource_digest
    role_policy = VALUE_POLICY if valuable else POLICY
    if not pending_decision:
        pending_decision = legacy_navigation_decision(batch, handle.get('payload', {}).get('school_id', 0),
                                                     int(policy['evidence_max_bytes']))
        if pending_decision:
            pending_decision.update(material_hash=material_hash, cursor=cursor)
            checkpoint['route_pending_decision'] = pending_decision
            if handle:
                tasks.checkpoint(checkpoint)
            batch = batch[:pending_decision['batch_count']]
    if pending_decision and (list(map(str, pending_decision.get('binding', []))) !=
                             [str(binding['id']), str(binding['version'])]
                             or pending_decision.get('skill_digest') != skill_digest):
        return {'status': 'needs_recovery', 'error_code': 'execution_conflict',
                'reviewed_routes': cursor, 'cached_routes': cached_count}
    cache_keys = {id(link): navigation_roles.candidate_key(site, link, link.get('_navigation_material'),
                  binding, skill_digest, role_policy) for link, _ in batch}
    cached = navigation_roles.load(cache_keys.values())
    evidence = {'school_id': handle.get('payload', {}).get('school_id', 0), 'candidates': [], 'evidence': []}
    mapping, reused, processed = {}, [], []
    for link, context in batch:
        saved_row = cached.get(cache_keys[id(link)])
        if saved_row and navigation_roles.supported(saved_row, link.get('_navigation_material')):
            reused.append((link, saved_row))
            processed.append(link)
            continue
        ident = append_navigation_evidence(evidence, link, context)
        mapping[ident] = link
        if len(canonical(evidence).encode()) > int(policy['evidence_max_bytes']):
            evidence['candidates'].pop(); evidence['evidence'].pop(); mapping.pop(ident)
            break
        processed.append(link)
    batch_count = len(processed)
    if pending_decision:
        evidence = pending_decision['evidence']
        observed = {hashlib.sha256(link['url'].encode()).hexdigest()[:20]: link for link, _ in batch}
        requested = [row['candidate_id'] for row in evidence['candidates']]
        if any(ident not in observed for ident in requested):
            return {'status': 'needs_recovery', 'error_code': 'execution_conflict',
                    'reviewed_routes': cursor, 'cached_routes': cached_count}
        mapping = {ident: observed[ident] for ident in requested}
        batch_count = pending_decision['batch_count']
    if not batch_count:
        return {'status': 'needs_recovery', 'error_code': 'navigation_evidence_too_large',
                'reviewed_routes': cursor, 'cached_routes': cached_count, 'remaining_routes': len(candidates) - cursor}
    admitted = list(checkpoint.get('admitted_routes', []))
    unresolved = list(checkpoint.get('unresolved_routes', []))
    # Free, evidence-matched routes must remain usable even when another item
    # in this slice cannot buy a model decision. Keep the cursor before the
    # undecided item; saved roles make replay idempotent without losing it.
    cached_new = 0
    for link, row in reused:
        from backend.scraper.http_client import same_school_url
        if row['role'] == 'unknown':
            saved = observed_probe(site, link)
        else:
            kind = {'directory': 'directory', 'unit': 'unit', 'publishing': 'channel', 'gateway': 'gateway'}[row['role']]
            saved = {'url': link['url'], 'kind': 'channel' if link.get('publication_route') else kind,
                     'decision': 'follow' if same_school_url(link['url'], site['root_url']) else 'official_external_link'}
        saved.update(ai_reviewed=True, ai_value=row['value'], ai_role=row['role'])
        apply_route(parsed, link, saved)
        if not any(value['url'] == link['url'] for value in admitted):
            admitted.append(saved)
            cached_new += 1
    if reused:
        checkpoint.update(admitted_routes=admitted, unresolved_routes=unresolved, route_cursor=cursor,
                          cached_navigation_routes=checkpoint.get('cached_navigation_routes', 0) + cached_new)
        if handle:
            tasks.checkpoint(checkpoint)
    response, fingerprint = None, None
    if mapping:
        record_progress(ai_state='running')
        try:
            fingerprint = (pending_decision['fingerprint'] if pending_decision else
                digest({'policy': role_policy, 'skill': skill_digest,
                        'evidence': evidence, 'binding': [binding['id'], binding['version']]}))
            if decision_allowed is not None and not decision_allowed('routes:' + fingerprint):
                return {'status': 'branch_stopped', 'error_code': 'no_discovery_progress',
                        'remaining_routes': len(candidates) - cursor,
                        'deferred_routes': len(candidates) - cursor,
                        'reviewed_routes': cursor,
                        'cached_routes': cached_count + checkpoint.get('cached_navigation_routes', 0)}
            # Persist before dispatch: a process interruption or later cache
            # hit must not change the identity of a possibly paid request.
            checkpoint['route_pending_decision'] = {'material_hash': material_hash, 'cursor': cursor,
                'batch_count': batch_count, 'binding': [binding['id'], binding['version']],
                'skill_digest': skill_digest, 'fingerprint': fingerprint, 'evidence': evidence}
            if handle:
                tasks.checkpoint(checkpoint)
            response = run_skill('student-information', 'navigation', evidence, 'directory', 'routes:' + fingerprint, binding=binding)
        except AIConfigError as exc:
            if exc.code in ('concurrency_limit', 'budget_exhausted'):
                # Reservation failures rolled back before dispatch, so these
                # known-unpaid plans may adapt to newly available evidence.
                checkpoint.pop('route_pending_decision', None)
                if handle:
                    tasks.checkpoint(checkpoint)
            if exc.code == 'concurrency_limit':
                return {'status': 'pending', 'next_delay': 10}
            record_progress(ai_state='failed', ai_error_code=exc.code)
            return {'status': 'needs_recovery', 'error_code': exc.code}
        if response['status'] == 'pending':
            return {'status': 'pending', 'next_delay': 10}
        if response['status'] != 'succeeded':
            record_progress(ai_state='failed', ai_error_code=response.get('error_code', ''))
            return {'status': 'needs_recovery', 'error_code': response.get('error_code')}
    rows = [(mapping[row['candidate_id']], row) for row in response['output']['results']] if response else []
    to_cache = []
    for link, row in rows:
        kind = {'directory': 'directory', 'unit': 'unit', 'publishing': 'channel', 'gateway': 'gateway'}.get(row['role'])
        from backend.scraper.http_client import same_school_url
        decision = 'follow' if same_school_url(link['url'], site['root_url']) else 'official_external_link'
        if link.get('publication_route'):
            # Actual list samples outrank a model's interpretation of its name.
            kind = 'channel'
        if kind:
            saved = {'url': link['url'], 'kind': kind, 'decision': decision}
        elif row['role'] == 'unknown':
            # Both route policies advance by inspecting this observed target.
            # Uncertainty is never promoted to a unit, column or owner verdict.
            saved = observed_probe(site, link)
        elif valuable:
            # Existing readable routes stay usable even if a short description
            # suggests low student relevance. Value changes priority, not access.
            saved = {'url': link['url'], 'kind': link['kind'], 'decision':
                     link['decision'] if link['decision'] in ('follow', 'official_external_link') else 'route_reference'}
        else:
            saved = {'url': link['url'], 'kind': link['kind'], 'decision': 'route_reference'}
        saved.update(ai_reviewed=True, ai_value=row['value'], ai_role=row['role'])
        apply_route(parsed, link, saved)
        admitted = [value for value in admitted if value['url'] != saved['url']]
        admitted.append(saved)
        cache_row = navigation_roles.cacheable_result(row, link.get('_navigation_material'))
        if cache_row:
            to_cache.append((cache_keys[id(link)], cache_row))
    navigation_roles.save(to_cache)
    checkpoint.pop('route_pending_decision', None)
    if handle:
        # The caller persists admitted links before deferring; replaying this
        # slice is harmless and the stable AI execution prevents double billing.
        checkpoint.update(route_cursor=cursor + batch_count, admitted_routes=admitted, unresolved_routes=unresolved,
                          cached_navigation_routes=checkpoint.get('cached_navigation_routes', 0))
        if fingerprint:
            # Persist the decision before the caller reacquires its writer lock.
            # Contention or a crash there cannot remove a paid call from the
            # shared branch clock on the next task claim.
            unrecorded = list(checkpoint.get('navigation_unrecorded_decisions', []))
            decision_id = 'routes:' + fingerprint
            if decision_id not in unrecorded:
                unrecorded.append(decision_id)
            checkpoint['navigation_unrecorded_decisions'] = unrecorded
        tasks.checkpoint(checkpoint)
    record_progress(ai_state='succeeded')
    return {'status': 'pending' if cursor + batch_count < len(candidates) else 'needs_recovery' if unresolved else 'processed',
            'unresolved_routes': unresolved, 'deferred_routes': 0,
            'remaining_routes': max(0, len(candidates) - cursor - batch_count),
            'reviewed_routes': cursor + batch_count,
            'cached_routes': cached_count + checkpoint.get('cached_navigation_routes', 0),
            **({'decision_id': 'routes:' + fingerprint} if fingerprint else {})}
