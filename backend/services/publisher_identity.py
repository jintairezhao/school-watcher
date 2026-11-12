"""Observed publisher declarations, separate from directories and administration."""
from urllib.parse import urljoin
import json
import re

from bs4 import Tag

from backend.services.source_ownership import (branding_candidates, normalized_name,
    profile_identity_candidates, profile_subject_matches, PROFILE_LABELS)
from backend.services.source_inventory import canonical_url

PREFIX = 'publisher_identity_observation:'
HOME_LABELS = frozenset(('首页', '学院首页', '中心首页', '单位首页', '本站首页'))
PROFILE_BRIDGE_LABELS = frozenset(('机构设置', '组织机构'))
MAX_PROFILE_ROUTE_HOPS = 4
DECLARATION = 'website_publisher_declaration'


def home_anchor_kind(anchor):
    """Classify an actual home anchor without inventing its visible label."""
    if not isinstance(anchor, Tag) or anchor.name != 'a' or not anchor.get('href'):
        return None
    if any(node.has_attr('hidden') or 'display:none' in re.sub(r'\s+', '', node.get('style', '')).lower()
           for node in (anchor, *anchor.parents) if getattr(node, 'attrs', None) is not None):
        return None
    text = anchor.get_text(' ', strip=True)
    if text in HOME_LABELS:
        return 'text'
    image = anchor.find('img', src=True)
    header = anchor.find_parent('header')
    if (text or image is None or header is None or anchor.find_parent(['article', 'main', 'blockquote', 'q'])
            or image.has_attr('hidden') or 'display:none' in re.sub(r'\s+', '', image.get('style', '')).lower()):
        return None
    containers = [anchor]
    for parent in anchor.parents:
        containers.append(parent)
        if parent is header:
            break
    if any(re.search(r'(?:^|[\s_-])(?:logo|brand)(?:$|[\s_-])',
            ' '.join(node.get('class', [])) + ' ' + node.get('id', ''), re.I) for node in containers):
        return 'header_logo'
    return None


def is_home_route(route):
    return route.get('label') in HOME_LABELS or route.get('home_link_kind') == 'header_logo'


def observations(soup, school, page_url, *, allow_declaration=False):
    from backend.scraper.discovery.structure import locator, node_key
    from backend.services.source_inventory import resolve_page_link
    from backend.scraper.article_urls import ARTICLE
    names, identities = set(), []
    school = normalized_name(school)
    for candidate in branding_candidates(soup):
        text = normalized_name(candidate['identity'])
        if not text.startswith(school):
            continue
        name = text[len(school):]
        name = re.sub(r'(?:官方网站|门户网站|首页|官网|网站)$', '', name)
        if re.fullmatch(r'[\u3400-\u9fff]{2,45}(?:学院|中心|研究院|研究所|学部|部门|办公室|图书馆)', name):
            names.add(name)
            identities.append((name, candidate))
    nodes = []
    if allow_declaration and not ARTICLE.search(page_url) and len(names) == 1:
        name, claim = identities[0]
        nodes.append(dict(key=node_key('publisher_declaration', name, page_url + '#' + claim['locator']),
            name=name, kind='unit', url=page_url, parent='', locator=claim['locator'], relation=DECLARATION))
    profile_names = list(profile_identity_candidates(soup))
    for body in soup.select('.c-right-detail__content, .v_news_content, .wp_articlecontent, article .entry-content'):
        for node in body.select('p,figcaption,caption,h1,h2,h3,h4'):
            text = node.get_text(' ', strip=True)
            match = re.fullmatch(r'([\u3400-\u9fff]{2,45}(?:学院|中心|研究院|研究所))(?:总体架构图|组织架构图)', text)
            if match:
                item = dict(source='unit_profile_caption', entity_name=match[1], identity=text,
                    locator=locator(node), container=locator(body))
                if not any(old['locator'] == item['locator'] for old in profile_names):
                    profile_names.append(item)
    base = soup.find('base', href=True)
    base_url = urljoin(page_url, base['href']) if base else page_url
    routes = []
    for anchor in soup.select('a[href]'):
        label = anchor.get_text(' ', strip=True)
        home_kind = home_anchor_kind(anchor)
        if label not in PROFILE_LABELS | HOME_LABELS | PROFILE_BRIDGE_LABELS and not home_kind:
            continue
        if not any(isinstance(parent, Tag) and (parent.name in ('header', 'nav') or
                label in PROFILE_LABELS and 'sub-left' in parent.get('class', []) or re.search(
                r'header|nav|menu', ' '.join(parent.get('class', [])) + ' ' + parent.get('id', ''), re.I))
                for parent in anchor.parents):
            continue
        try:
            target = resolve_page_link(base_url, anchor['href'])
        except ValueError:
            continue
        if target:
            routes.append(dict(label=label, target_url=target, locator=locator(anchor),
                **({'home_link_kind': home_kind} if home_kind == 'header_logo' else {})))
    return dict(nodes=nodes, profile_names=profile_names, routes=routes)


def saved_observation(page, key):
    for note in json.loads(page.get('notes_json') or '[]'):
        if not isinstance(note, str) or not note.startswith(PREFIX):
            continue
        try:
            data = json.loads(note[len(PREFIX):])
            if (data['site_key'] == key and data['reference_url'] == page['url']
                    and data['final_url'] == (page.get('final_url') or page['url'])
                    and data['content_hash'] == page.get('content_hash')):
                return data
        except (KeyError, TypeError, ValueError):
            continue
    return {}


def routes(page, key):
    from backend.services.source_directory_reconciliation import _navigations
    found = [dict(label=link['name'], target_url=link['url'], locator=link['locator'])
             for link in _navigations(page, key)]
    found += saved_observation(page, key).get('routes', [])
    found += [dict(label=e.get('label', ''), target_url=e['target_url'], locator=e['locator'])
        for e in page.get('navigation_edges', []) if e.get('parent_url') == page['url']
        and e.get('content_hash') == page.get('content_hash') and e.get('observed_at')
        and e.get('target_url') and e.get('locator')]
    return found


def _profile_endpoint(label, page, key):
    if label in PROFILE_LABELS:
        return True
    # A structure menu can lead directly to an actual profile. Its label alone
    # is insufficient; the target must have a current explicit self-description.
    return label in PROFILE_BRIDGE_LABELS and any(
        candidate.get('source') == 'unit_profile_self_description' and candidate.get('entity_name')
        for candidate in saved_observation(page, key).get('profile_names', []))


def profile_access_routes(relations, home, *, scope_url=None):
    """Shortest routes first; only retained profile/structure navigation, no fetch."""
    from collections import deque
    from backend.services.source_placements import _within_unit
    pending = deque([(home['url'], [], {home['url']})])
    emitted = set()
    while pending:
        origin, trail, visited = pending.popleft()
        page = relations.pages.get(origin)
        if (not page or page.get('state') != 'fetched' or not page.get('content_hash')
                or not page.get('checked_at') or len(trail) >= MAX_PROFILE_ROUTE_HOPS):
            continue
        seen = set()
        for entry in routes(page, relations.site['site_key']):
            label, target, position = (entry.get(field) for field in ('label', 'target_url', 'locator'))
            if label not in PROFILE_LABELS | PROFILE_BRIDGE_LABELS or not target or not position:
                continue
            signature = (label, target, position)
            if signature in seen or target in visited or not _within_unit(target, scope_url or home['url'], relations.site['root_url']):
                continue
            seen.add(signature)
            target_page = relations.pages.get(target)
            if (not target_page or target_page.get('state') != 'fetched' or not target_page.get('content_hash')
                    or not target_page.get('checked_at')):
                continue
            chain = trail + [dict(reference_url=origin, target_url=target, label=label, locator=position,
                content_hash=page['content_hash'], observed_at=page['checked_at'])]
            if _profile_endpoint(label, target_page, relations.site['site_key']) and target not in emitted:
                emitted.add(target)
                yield dict(profile_url=target, hops=chain)
            pending.append((target, chain, visited | {target}))


def checked_profile_route(home, profile, entry_locator, trail, pages, root_url, *, scope_url=None):
    """Validate a saved route's direction and material versions without HTML."""
    from backend.services.source_placements import _within_unit
    legacy = trail is None
    if legacy:
        page = pages.get(home) or {}
        trail = [dict(reference_url=home, target_url=profile, locator=entry_locator,
            content_hash=page.get('content_hash'), observed_at=page.get('checked_at'))]
    if not isinstance(trail, list) or not 1 <= len(trail) <= MAX_PROFILE_ROUTE_HOPS:
        return None
    current, visited = home, {home}
    for index, step in enumerate(trail):
        if not isinstance(step, dict) or not all(isinstance(step.get(field), str) and step[field]
                for field in ('reference_url', 'target_url', 'locator', 'content_hash', 'observed_at')):
            return None
        origin, target = step.get('reference_url'), step.get('target_url')
        page, destination = pages.get(origin) or {}, pages.get(target) or {}
        from backend.services.source_inventory import site_key
        allowed = (_profile_endpoint(step.get('label'), destination, site_key(root_url))
                   if index == len(trail)-1 else step.get('label') in PROFILE_LABELS | PROFILE_BRIDGE_LABELS)
        if (origin != current or not target or target in visited or not step.get('locator')
                or not legacy and not allowed
                or not _within_unit(target, scope_url or home, root_url)
                or page.get('state') != 'fetched' or not page.get('content_hash') or not page.get('checked_at')
                or destination.get('state') != 'fetched' or not destination.get('content_hash') or not destination.get('checked_at')
                or step.get('content_hash') != page['content_hash']):
            return None
        current = target
        visited.add(target)
    return trail if current == profile and trail[0]['locator'] == entry_locator else None


def verify_profile_route(home, profile, entry_locator, trail, material, relations, *, scope_url=None):
    """Execute every saved forward link against its own retained original."""
    import hashlib
    from bs4 import BeautifulSoup
    from backend.scraper.discovery.structure import locator_element
    from backend.services.source_inventory import resolve_page_link
    checked = checked_profile_route(home, profile, entry_locator, trail, relations.pages, relations.site['root_url'],scope_url=scope_url)
    if not checked:
        return None
    proofs = []
    for index, step in enumerate(checked):
        origin = step['reference_url']
        page = relations.pages[origin]
        raw = material(origin)
        if not raw or hashlib.sha256(raw.encode()).hexdigest() != step['content_hash']:
            return None
        soup = BeautifulSoup(raw, 'lxml')
        node = locator_element(soup, step['locator'])
        label = node.get_text(' ', strip=True) if node is not None else ''
        endpoint = index == len(checked)-1
        destination = relations.pages[step['target_url']]
        allowed = (_profile_endpoint(label, destination, relations.site['site_key'])
                   if endpoint else label in PROFILE_LABELS | PROFILE_BRIDGE_LABELS)
        if node is None or node.name != 'a' or not node.get('href') or not allowed:
            return None
        if endpoint and label in PROFILE_BRIDGE_LABELS:
            target_raw = material(step['target_url'])
            if (not target_raw or hashlib.sha256(target_raw.encode()).hexdigest() != destination['content_hash']
                    or not profile_identity_candidates(BeautifulSoup(target_raw, 'lxml'))):
                return None
        if step.get('label') is not None and node.get_text(' ', strip=True) != step['label']:
            return None
        base = soup.find('base', href=True)
        response = page.get('final_url') or origin
        try:
            if resolve_page_link(urljoin(response, base['href']) if base else response, node['href']) != step['target_url']:
                return None
        except ValueError:
            return None
        proofs.append(dict(reference_url=origin, locator=step['locator'], content_hash=step['content_hash'],
            observed_at=step['observed_at']))
    return proofs


def independent_paths(relations, records, owners):
    """Build candidates only. The writer rechecks every role on original DOMs."""
    from backend.services.source_placements import _within_unit
    result = []
    key = relations.site.get('site_key')
    if not key:
        # A partial report can still expose candidate lists. It cannot prove
        # publisher identity without the catalog identity used by the writer.
        return result
    root = canonical_url(relations.site['root_url'])
    for record in records:
        if record.get('relation') != DECLARATION:
            continue
        home = relations.pages.get(record['reference_url'])
        if (not home or home.get('state') != 'fetched' or home.get('content_hash') != record['content_hash']
                or 'unit_profile_page:' in (home.get('notes_json') or '')):
            continue
        owner = dict(unit_key=record['node_key'], unit_name=record['name'], nodes=[relations._node(record)],
            basis='official_website_entry', references=[], verified=False)
        identity = relations._unit_website_identity(home, owner)
        if not identity:
            continue
        # A separately proven roster identity already owns its declaration.
        if any(candidate['unit_name'] == record['name'] and (relations._unit_website_identity(home, candidate)
               or relations._profile_website_identity(home,candidate))
               for candidate in owners.get(home['url'], [])):
            continue
        for profile_route in profile_access_routes(relations, home):
            profile = relations.pages.get(profile_route['profile_url'])
            if (not profile or profile.get('state') != 'fetched' or not profile.get('content_hash')
                    or not _within_unit(profile['url'], home['url'], root)):
                continue
            claims = saved_observation(profile, key).get('profile_names', [])
            claims = [claim for claim in claims if profile_subject_matches(
                claim.get('entity_name'), record['name'], relations.site['name'])]
            from .profile_home_reference import home_return,return_references
            back = home_return(relations,home,profile,record['name'])
            if not claims or not back:
                continue
            for parent in relations.pages.values():
                if parent.get('state') != 'fetched' or not parent.get('content_hash'):
                    continue
                incoming = next((route for route in routes(parent, key)
                                 if canonical_url(route['target_url']) == canonical_url(home['url'])), None)
                if not incoming or parent['url'] == home['url']:
                    continue
                parent_owner = None
                if canonical_url(parent['url']) != root:
                    for candidate in owners.get(parent['url'], []):
                        proof = (relations._unit_website_identity(parent, candidate)
                                 or relations._profile_website_identity(parent, candidate))
                        if proof:
                            refs = ([relations._reference(parent['url'], [proof['profile_entry_locator']]),
                                     relations._reference(proof['reference_url'],
                                         [proof['locator'], proof['profile_home_locator']])]
                                    if proof.get('source') == 'unit_profile_self_description' else
                                    [relations._reference(parent['url'], [proof['locator']])])
                            if proof.get('source') == 'unit_profile_self_description':
                                refs += [relations._reference(hop['reference_url'], [hop['locator']])
                                         for hop in proof.get('profile_route', [])[1:]]
                                refs += return_references(relations,parent['url'],proof.get('profile_home_witness'))
                            parent_owner = dict(candidate, website_identity=proof,
                                references=candidate['references'] + refs)
                            break
                    if not parent_owner:
                        continue
                claim = claims[0]
                context = dict(kind=DECLARATION, node_key=record['node_key'], name=record['name'],
                    website_url=home['url'], identity_locator=record['locator'], identity_text=identity['identity'],
                    profile_url=profile['url'], profile_entry_locator=profile_route['hops'][0]['locator'],
                    profile_route=profile_route['hops'],
                    **back, profile_claim=claim,
                    entrance_reference_url=parent['url'], entrance_locator=incoming['locator'],
                    entrance_owner=parent_owner)
                references = [relations._reference(home['url'], [record['locator']]),
                    relations._reference(profile['url'], [claim['locator'], back['profile_home_locator']]),
                    relations._reference(parent['url'], [incoming['locator']])]
                references += [relations._reference(hop['reference_url'], [hop['locator']]) for hop in profile_route['hops']]
                references += return_references(relations,home['url'],back.get('profile_home_witness'))
                if parent_owner:
                    references += parent_owner['references']
                result.append(dict(owner, website_identity=identity, publisher_declaration=context,
                                   references=references))
    return result


def verify_declaration(path, material, relations):
    """Verify identity, provenance and profile without asserting a parent unit."""
    import hashlib
    from bs4 import BeautifulSoup
    from backend.scraper.discovery.structure import locator_element
    from backend.services.source_inventory import resolve_page_link
    from backend.services.source_ownership import website_identity_forms
    from backend.services.source_relationships import ROSTER_RELATIONS
    context = path.get('publisher_declaration') or {}
    home, profile, entrance = (context.get(field) for field in
        ('website_url', 'profile_url', 'entrance_reference_url'))
    if (context.get('kind') != DECLARATION or context.get('node_key') != path.get('unit_key')
            or context.get('name') != path.get('unit_name')):
        return None
    documents = {}
    def document(url):
        if url in documents:
            return documents[url]
        page = relations.pages.get(url)
        raw = material(url) if page else ''
        if (not page or page.get('state') != 'fetched' or not raw or not page.get('checked_at')
                or hashlib.sha256(raw.encode()).hexdigest() != page.get('content_hash')):
            return None
        documents[url] = BeautifulSoup(raw, 'lxml')
        return documents[url]
    if not all(document(url) is not None for url in (home, profile, entrance)):
        return None
    declared = observations(documents[home], relations.site['name'], home, allow_declaration=True)
    if not any(node['key'] == context['node_key'] and node['name'] == context['name']
               and node['locator'] == context.get('identity_locator') for node in declared['nodes']):
        return None
    claim = context.get('profile_claim') or {}
    observed_names = observations(documents[profile], relations.site['name'], profile)['profile_names']
    if claim not in observed_names or not profile_subject_matches(
            claim.get('entity_name'), context['name'], relations.site['name']):
        return None
    def actual_link(origin, position, target, labels=None, *, home_link=False):
        soup = document(origin)
        node = locator_element(soup, position) if soup else None
        if node is None or node.name != 'a' or not node.get('href'):
            return False
        if labels is not None and node.get_text(' ', strip=True) not in labels:
            return False
        if home_link and not home_anchor_kind(node):
            return False
        base = soup.find('base', href=True)
        try:
            return resolve_page_link(urljoin(origin, base['href']) if base else origin, node['href']) == target
        except ValueError:
            return False
    route_proofs = verify_profile_route(home, profile, context.get('profile_entry_locator', ''),
        context.get('profile_route'), material, relations)
    from .profile_home_reference import verify_home_return,return_references
    home_witness=context.get('profile_home_witness')
    home_proofs=(verify_home_return(home,profile,context.get('profile_home_locator'),home_witness,
        context['name'],relations,material=material) if home_witness else None)
    if (not route_proofs or (home_witness and not home_proofs)
            or not home_witness and not actual_link(profile, context.get('profile_home_locator', ''), home, home_link=True)
            or not actual_link(entrance, context.get('entrance_locator', ''), home)):
        return None
    if canonical_url(entrance) == canonical_url(relations.site['root_url']):
        title = documents[entrance].title
        if not title or normalized_name(relations.site['name']) not in website_identity_forms(title.get_text(' ', strip=True)):
            return None
    else:
        parent = context.get('entrance_owner') or {}
        identity = parent.get('website_identity') or {}
        unit = next((n for n in reversed(parent.get('nodes', []))
                     if n.get('kind') == 'unit' and n.get('relation') in ROSTER_RELATIONS), None)
        if not unit or entrance not in relations.website_addresses(parent):
            return None
        roster_proved = False
        from backend.services.school_structure import _directory_website_assertion, _profile_identity_assertion
        if parent.get('website_binding'):
            roster_proved = bool(_directory_website_assertion(
                parent['website_binding'], parent['unit_name'], material, relations))
        else:
            for reference in parent.get('references', []):
                page = relations.pages.get(reference['url']) or {}
                if page.get('kind') != 'directory' or document(reference['url']) is None:
                    continue
                for position in reference.get('locators', []):
                    element = locator_element(documents[reference['url']], position)
                    if element is None or any(p.name in ('header', 'nav', 'footer') for p in element.parents):
                        continue
                    anchors = [element] if element.name == 'a' else element.select('a[href]')
                    if any(normalized_name(a.get_text(' ', strip=True)) == normalized_name(parent['unit_name'])
                           and actual_link(reference['url'], _locator(a), entrance) for a in anchors):
                        roster_proved = True
        if not roster_proved:
            return None
        if identity.get('source') == 'unit_profile_self_description':
            if not _profile_identity_assertion(identity, parent, material, relations):
                return None
        else:
            if not relations._unit_website_identity(relations.pages[entrance], parent):
                return None
            element = locator_element(documents[entrance], identity.get('locator', ''))
            text = ((element.get('content') if element.name == 'meta' else element.get('alt')
                     if element.name == 'img' else element.get_text(' ', strip=True)) if element else '')
            if text != identity.get('identity'):
                return None
    # Source paths can also collect optional navigation detours. Missing raw
    # material for an unrelated sibling must not erase the executed identity
    # witnesses. Keep this proof limited to its explicit roles.
    identity_references = [
        dict(url=home, locators=[context['identity_locator']]),
        dict(url=profile, locators=[claim['locator'], context['profile_home_locator']]),
        dict(url=entrance, locators=[context['entrance_locator']]),
    ]
    identity_references += [dict(url=proof['reference_url'], locators=[proof['locator']]) for proof in route_proofs]
    identity_references += return_references(relations,home,home_witness)
    identity_references += (context.get('entrance_owner') or {}).get('references', [])
    evidence = []
    for reference in identity_references:
        page = relations.pages.get(reference['url']) or {}
        if document(reference['url']) is None:
            return None
        for position in reference.get('locators', []):
            item = dict(reference_url=reference['url'], locator=position,
                content_hash=page['content_hash'], observed_at=page['checked_at'])
            if item not in evidence:
                evidence.append(item)
    return dict(schema=1, writer='official_structure', kind=DECLARATION, node_key=context['node_key'],
        name=context['name'], reference_url=home, website_url=home, locator=context['identity_locator'],
        profile_url=profile, profile_locator=claim['locator'], entrance_reference_url=entrance,
        entrance_locator=context['entrance_locator'], administrative_parent_asserted=False,
        statement=context, evidence=evidence)


def _locator(node):
    from backend.scraper.discovery.structure import locator
    return locator(node)
