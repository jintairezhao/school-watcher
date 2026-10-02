"""Join publication entrances to current official roster evidence, without guessing ownership.

These are website paths, not an administrative or publishing-responsibility verdict.
Names, discovery paths and URL prefixes never join unrelated records.
"""
from collections import defaultdict
import json
import re
from urllib.parse import urlsplit

from backend.scraper.http_client import same_school_url
from backend.services.source_inventory import canonical_url
from backend.services.source_ownership import PREFIX, BRANDING_PREFIX, domain_evidence, normalized_name, website_identity_forms
from backend.services.student_sources import TEACHING

ROSTER_RELATIONS = {'directory_entry', 'directory_entry_no_link', 'nested_directory_entry',
                    'sub_unit', 'attached_unit', 'same_table_row', 'non_entity_directory_entry', 'campus_directory_entry',
                    'same_directory_row', 'directory_companion_entry'}
PATH_RELATIONS = ROSTER_RELATIONS | {'shared_table_row', 'menu_group', 'page_identity', 'campus_group',
                                   'academic_group', 'shared_directory_row', 'shared_directory_label', 'directory_group',
                                   'publication_group'}


def _page_address(url):
    parts = urlsplit(canonical_url(url))
    return parts.hostname, parts.port, parts.path, parts.query, parts.fragment


def _same_listing(a, b):
    samples = {_page_address(s['url']) for s in a.get('samples', [])}
    return (bool(a.get('name')) and a.get('name') == b.get('name') and
            len(samples & {_page_address(s['url']) for s in b.get('samples', [])}) >= 2)


def teaching_entrance(node):
    return node['kind'] == 'group' and len(node['name']) <= 18 and bool(TEACHING.search(node['name']))


class SourceRelationships:
    def __init__(self, report, records, *, directory_navigation=False):
        self.entries = defaultdict(list)
        self.pages = {}
        self.aliases = defaultdict(set)
        self.alias_references = defaultdict(list)
        self.site = report['site'] if report else {}
        self.ownership_pages = [p for p in (report or {}).get('pages', [])
                                if PREFIX in (p.get('notes_json') or '')]
        if not report:
            return
        # Only an observed, successful redirect can connect different URLs.
        for page in report['pages']:
            self.pages[page['url']] = page
            if page['state'] == 'fetched' and page.get('final_url') and page.get('health') != 'dynamic_content':
                a, b = canonical_url(page['url']), canonical_url(page['final_url'])
                self.aliases[a].add(b)
                self.aliases[b].add(a)
        # A CMS can serve one column at its navigation URL and at a pager URL
        # without redirecting. Both fetched lists must agree on their articles,
        # heading and website identity; a copied breadcrumb is insufficient.
        by_address = {canonical_url(p.get('final_url') or p['url']): p for p in report['pages']
                      if p['state'] == 'fetched' and p.get('feed_json') and p.get('content_hash')}
        protocol_variants = defaultdict(list)
        for address, page in by_address.items():
            for other_address, other in protocol_variants[_page_address(address)]:
                if (not self._school_page(address) or not page.get('title') or
                        normalized_name(page['title']) != normalized_name(other.get('title', ''))):
                    continue
                pair = next(((a,b) for a in json.loads(page['feed_json']).get('lists', [])
                    for b in json.loads(other['feed_json']).get('lists', []) if _same_listing(a,b)), None)
                if pair:
                    self.aliases[address].add(other_address); self.aliases[other_address].add(address)
                    refs = [self._reference(p['url'], [f['heading_locator'], f['container_locator']])
                            for p,f in zip((page,other),pair)]
                    self.alias_references[address].extend(refs)
                    self.alias_references[other_address].extend(refs)
            protocol_variants[_page_address(address)].append((address,page))
        for address, page in by_address.items():
            for feed in json.loads(page['feed_json']).get('lists', []):
                alias = feed.get('listing_alias') or {}
                target = canonical_url(alias.get('canonical_url', ''))
                other = by_address.get(target)
                if (not other or alias.get('url') != address or not self._school_page(address)
                        or not self._school_page(target) or not page.get('title')
                        or normalized_name(page['title']) != normalized_name(other.get('title', ''))):
                    continue
                matched = next((f for f in json.loads(other['feed_json']).get('lists', [])
                    if _same_listing(feed,f)), None)
                if not matched:
                    continue
                self.aliases[address].add(target); self.aliases[target].add(address)
                refs = [self._reference(page['url'], [alias['breadcrumb_locator'], alias['script_locator']]),
                        self._reference(other['url'], [matched['heading_locator'], matched['container_locator']])]
                self.alias_references[address].extend(refs)
                self.alias_references[target].extend(refs)
        by_reference = defaultdict(list)
        for record in records:
            page = self.pages.get(record['reference_url'])
            if (page and page['state'] == 'fetched' and page.get('content_hash')
                    and record['content_hash'] == page['content_hash']
                    and self._school_page(page.get('final_url') or page['url'])):
                by_reference[record['reference_url']].append(record)
        publication_pages = set()
        major_directory_pages = set()
        for reference in by_reference:
            if self.pages[reference].get('feed_json'):
                publication_pages.update(self._addresses(reference))
            if any(n['kind'] == 'major' and n['relation'] == 'major_directory_entry'
                   for n in by_reference[reference]):
                major_directory_pages.update(self._addresses(reference))

        owners = defaultdict(list)
        for reference, nodes in by_reference.items():
            by_key = defaultdict(list)
            for node in nodes:
                by_key[node['node_key']].append(node)
            for node in nodes:
                if node['kind'] != 'unit' or node['relation'] not in ROSTER_RELATIONS or not node['url']:
                    continue
                for chain in self._roster_paths(node, by_key):
                    evidence = {'unit_key': node['node_key'], 'unit_name': node['name'],
                                'nodes': [self._node(n) for n in chain],
                                'references': [self._reference(reference, [n['locator'] for n in chain])],
                                'basis': 'official_website_entry', 'verified': False}
                    for address in self._addresses(node['url']):
                        owners[address].append(evidence)

        root = self.site.get('root_url', '')
        for reference, nodes in by_reference.items():
            page = self.pages[reference]
            own_paths = []
            for address in self._addresses(reference):
                for owner in owners.get(address, []):
                    identity = self._unit_website_identity(page, owner)
                    if identity:
                        own_paths.append(dict(owner, website_identity=identity,
                            references=owner['references'] + [self._reference(reference, [identity['locator']])]))
                    elif directory_navigation and 'unit_profile_page:' not in (page.get('notes_json') or ''):
                        # An official directory can establish a website placement
                        # even when that site's branding is stale. This weaker
                        # basis cannot establish a publishing owner or activation.
                        own_paths.append(dict(owner, basis='official_directory_navigation',
                            identity_pending=True, references=owner['references'] +
                            [self._reference(reference, ['title'])]))
            if self._addresses(reference) & self._addresses(root):
                own_paths.append({'unit_key': 'school:' + self.site['site_key'],
                                  'unit_name': self.site['name'], 'nodes': [], 'references': [],
                                  'basis': 'school_website_entry', 'verified': False})
            if not own_paths:
                continue
            by_key = defaultdict(list)
            for node in nodes:
                by_key[node['node_key']].append(node)
            for owner in own_paths:
                # A column extracted from a unit's own homepage shares that entrance.
                self._add(reference, owner, reference, [], '')
                for node in nodes:
                    if (node['url'] and (node['kind'] == 'channel' or teaching_entrance(node) or node['url'] in publication_pages
                                        or node['url'] in major_directory_pages) and
                            (node['relation'] in ('unit_channel', 'navigation_entry', 'publication_column') or
                             node['relation'] == 'linked_navigation_unverified' and teaching_entrance(node))):
                        for chain in self._roster_paths(node, by_key,
                                PATH_RELATIONS | {'unit_channel', 'navigation_entry', 'publication_column', 'linked_navigation_unverified'}):
                            ancestors = [n for n in chain[:-1] if n['relation'] != 'page_identity']
                            self._add(node['url'], owner, reference, [n['locator'] for n in chain],
                                      node['name'], [self._node(n) for n in ancestors])
                if page.get('feed_json'):
                    feeds = json.loads(page['feed_json'])
                    for feed in feeds.get('lists', [feeds]):
                        if feed.get('name') and feed.get('column_url') and feed.get('column_link_locator'):
                            locators = [feed['column_link_locator']]
                            if feed.get('column_script_locator'):
                                locators.append(feed['column_script_locator'])
                            groups = [self._node(n) for n in nodes if n['relation'] == 'publication_group'
                                      and n['name'] == feed.get('column_group_name')
                                      and n['locator'] == feed.get('column_group_locator')]
                            self._add(feed['column_url'], owner, reference,
                                      locators, feed['name'], groups)

        if directory_navigation:
            # A failed navigation endpoint does not erase the official unit
            # placement of a still-readable pager. This is deliberately not a
            # URL alias or publishing-owner verdict. Keep activation strict.
            unit_entrances = [owner for entrance, entries in list(self.entries.items()) for owner in entries
                if owner.get('website_identity') and (unit := next(
                    (n for n in reversed(owner['nodes']) if n['kind'] == 'unit'), None))
                and entrance in self._addresses(unit['url'])]
            for address, page in by_address.items():
                if 'unit_profile_page:' in (page.get('notes_json') or ''):
                    continue
                for feed in json.loads(page['feed_json']).get('lists', []):
                    alias = feed.get('listing_alias') or {}
                    target = canonical_url(alias.get('canonical_url', ''))
                    other = self.pages.get(target)
                    if (alias.get('url') != address or not self._school_page(address)
                            or other and other['state'] == 'fetched'):
                        continue
                    candidates = list(self.entries.get(target, []))
                    # An old aggregate column may have disappeared from the
                    # homepage menu. Its observed pager link, exact current
                    # website branding and scoped articles still locate the unit.
                    for owner in unit_entrances:
                        unit = next(n for n in reversed(owner['nodes']) if n['kind'] == 'unit')
                        navigation = urlsplit(target)
                        if any(navigation.netloc == (scope := urlsplit(u)).netloc
                            and not scope.query and not scope.fragment
                            and (navigation.path == scope.path.rstrip('/') or
                                 navigation.path.startswith(scope.path.rstrip('/') + '/'))
                            for u in self._addresses(unit['url'])):
                            candidates.append(owner)
                    for owner in candidates:
                        identity = self._unit_website_identity(page, owner)
                        unit = next((n for n in reversed(owner['nodes']) if n['kind'] == 'unit'), None)
                        if not identity or not unit:
                            continue
                        scopes = [urlsplit(u) for u in self._addresses(unit['url'])]
                        articles = {canonical_url(s['url']) for s in feed.get('samples', [])}
                        scoped = [url for url in articles if any(
                            (p := urlsplit(url)).netloc == scope.netloc and not scope.query and not scope.fragment
                            and (p.path == scope.path.rstrip('/') or p.path.startswith(scope.path.rstrip('/') + '/'))
                            for scope in scopes)]
                        if len(scoped) < 2 or len(scoped) != len(articles):
                            continue
                        evidence = dict(owner, basis='official_directory_navigation', website_identity=identity,
                            pagination_identity_pending=True, navigation_target_url=target)
                        self._add(address, evidence, page['url'],
                            [alias['breadcrumb_locator'], alias['script_locator'], identity['locator'],
                             feed['container_locator']], feed['name'], owner.get('entry_nodes', []))

        # Follow observed menus inside an identified unit website. Colleges
        # commonly put notices one level below a teaching/admissions landing
        # page; that extra page must not erase the evidenced college identity.
        # No URL is synthesized and cross-site sidebar links cannot propagate.
        propagated = set()
        from backend.scraper.article_urls import ARTICLE
        for _ in range(4):
            changed = False
            for reference, nodes in by_reference.items():
                page = self.pages[reference]
                if (page['kind'] not in ('channel', 'navigation', 'directory', 'unit') or
                        ARTICLE.search(reference) or 'unit_profile_page:' in (page.get('notes_json') or '')):
                    continue
                inbound = list(self.entries.get(canonical_url(reference), []))
                by_key = defaultdict(list)
                for node in nodes:
                    by_key[node['node_key']].append(node)
                for owner in inbound:
                    identity = (reference, owner['unit_key'], tuple(n['node_key'] for n in owner['nodes']))
                    if owner['basis'] != 'official_website_entry' or identity in propagated:
                        continue
                    unit = next((n for n in reversed(owner['nodes']) if n['kind'] == 'unit'), None)
                    target = urlsplit(page.get('final_url') or reference)
                    scopes = [urlsplit(address) for address in self._addresses(unit['url'])] if unit else []
                    if not any(target.netloc == scope.netloc and not scope.query and not scope.fragment and
                               (target.path == scope.path.rstrip('/') or target.path.startswith(scope.path.rstrip('/') + '/'))
                               for scope in scopes):
                        continue
                    propagated.add(identity)
                    for node in nodes:
                        if (not node['url'] or not (node['kind'] == 'channel' or teaching_entrance(node)) or
                                not (node['relation'] in ('unit_channel', 'navigation_entry', 'publication_column') or
                                     node['relation'] == 'linked_navigation_unverified' and teaching_entrance(node)) or
                                ARTICLE.search(node['url'])):
                            continue
                        for chain in self._roster_paths(node, by_key,
                                PATH_RELATIONS | {'unit_channel', 'navigation_entry', 'publication_column', 'linked_navigation_unverified'}):
                            ancestors = list(owner.get('entry_nodes', []))
                            entry_name = owner.get('entry_name', '')
                            if TEACHING.search(entry_name) and len(entry_name) <= 18:
                                ancestors.append({'node_key': 'entry:' + reference, 'name': entry_name,
                                    'kind': 'group', 'url': reference, 'relation': 'publication_group'})
                            ancestors.extend(self._node(n) for n in chain[:-1] if n['relation'] != 'page_identity')
                            ancestors = list({n['node_key']: n for n in ancestors}.values())
                            self._add(node['url'], owner, reference, [n['locator'] for n in chain],
                                      node['name'], ancestors)
                            changed = True
            if not changed:
                break

    def _school_page(self, url):
        return (same_school_url(url, self.site.get('root_url', '')) or
                bool(domain_evidence(self.ownership_pages, self.site.get('site_key'), url)))

    def _unit_website(self, page, owner):
        return bool(self._unit_website_identity(page, owner))

    def _unit_website_identity(self, page, owner):
        if 'unit_profile_page:' in (page.get('notes_json') or ''):
            return None
        # A department's introduction article often repeats its college's whole
        # header. That does not make every college column a department column.
        unit = normalized_name(re.sub(r'[（(]共建[）)]$', '', owner['unit_name']))
        school = normalized_name(self.site['name'])
        names = {unit if unit.startswith(school) else school + unit}
        if same_school_url(page.get('final_url') or page['url'], self.site['root_url']):
            names.add(unit)
        identities = {name + suffix for name in names
                      for suffix in ('', '首页', '官网', '官方网站', '网站', '门户网站')}
        candidates = []
        for note in json.loads(page.get('notes_json') or '[]'):
            if not isinstance(note, str) or not note.startswith(BRANDING_PREFIX):
                continue
            try:
                evidence = json.loads(note[len(BRANDING_PREFIX):])
                if (evidence['site_key'] != self.site['site_key'] or
                        evidence['reference_url'] != page['url'] or
                        evidence['final_url'] != (page.get('final_url') or page['url']) or
                        evidence['content_hash'] != page.get('content_hash')):
                    continue
                candidates.extend(c for c in evidence['candidates'] if isinstance(c, dict)
                    and c.get('source') in ('title', 'og:site_name', 'header_logo_alt')
                    and isinstance(c.get('identity'), str) and isinstance(c.get('locator'), str)
                    and c['locator'])
            except (ValueError, TypeError, KeyError):
                continue
        # Older snapshots still retain their exact document title.
        candidates.append({'source': 'title', 'identity': page.get('title') or '', 'locator': 'title'})
        return next((c for c in candidates if website_identity_forms(c['identity']) & identities), None)

    def _addresses(self, url):
        if not url:
            return set()
        pending, seen = [canonical_url(url)], set()
        while pending:
            address = pending.pop()
            if address in seen:
                continue
            seen.add(address)
            pending.extend(self.aliases.get(address, set()) - seen)
        return seen

    @staticmethod
    def _roster_paths(leaf, by_key, allowed=PATH_RELATIONS):
        pending, emitted = [(leaf, [], set())], set()
        while pending:
            node, tail, visited = pending.pop()
            key = node['node_key']
            if key in visited or node['relation'] not in allowed:
                continue
            chain = [node] + tail
            if not node['parent_key']:
                signature = tuple((n['node_key'], n['parent_key'], n['relation']) for n in chain)
                if signature not in emitted:
                    emitted.add(signature)
                    yield chain
            else:
                for parent in by_key.get(node['parent_key'], []):
                    pending.append((parent, chain, visited | {key}))

    @staticmethod
    def _node(node):
        return {key: node[key] for key in ('node_key', 'name', 'kind', 'url', 'relation')}

    def _reference(self, url, locators):
        page = self.pages[url]
        return {'url': url, 'title': page.get('title') or page['label'],
                'content_hash': page['content_hash'], 'checked_at': page['checked_at'],
                'locators': sorted(set(locators))}

    def _add(self, target, owner, reference, locators, entry_name, entry_nodes=()):
        if not self._school_page(target):
            return
        evidence = dict(owner, references=owner['references'] + [self._reference(reference, locators)],
                        entry_name=entry_name, entry_url=target, entry_nodes=list(entry_nodes))
        for address in self._addresses(target):
            self.entries[address].append(dict(evidence,
                references=evidence['references'] + (self.alias_references[address]
                    if address != canonical_url(target) else [])))

    def paths_for(self, url):
        result = {}
        for evidence in self.entries.get(canonical_url(url or ''), []):
            signature = (evidence['unit_key'], tuple((n['node_key'], n['relation']) for n in evidence['nodes']),
                         tuple(n['node_key'] for n in evidence['entry_nodes']))
            if signature not in result:
                result[signature] = dict(evidence, references=[], entry_urls=[], entry_names=[])
            saved = result[signature]
            if evidence['entry_url'] not in saved['entry_urls']:
                saved['entry_urls'].append(evidence['entry_url'])
            if evidence['entry_name'] and evidence['entry_name'] not in saved['entry_names']:
                saved['entry_names'].append(evidence['entry_name'])
            for additional in evidence['references']:
                existing = next((r for r in saved['references'] if r['url'] == additional['url']
                                 and r['content_hash'] == additional['content_hash']), None)
                if existing:
                    existing['locators'] = sorted(set(existing['locators'] + additional['locators']))
                else:
                    saved['references'].append(dict(additional))
        return list(result.values())

    def publication_owners(self, url, paths):
        """Navigation can point outside a unit; only its own site supplies an owner.

        Keep the complete paths as provenance, but don't turn a college's link
        to the graduate school into a claim to the graduate school's articles.
        URL scope only filters an already evidenced relationship.
        """
        targets = [urlsplit(address) for address in self._addresses(url)]
        names = set()
        for path in paths:
            if path.get('basis') != 'official_website_entry':
                continue
            unit = next((n for n in reversed(path['nodes']) if n['kind'] == 'unit'), None)
            if not unit:
                continue
            for address in self._addresses(unit['url']):
                scope = urlsplit(address)
                if any(target.netloc == scope.netloc and
                       ((target.path, target.query, target.fragment) == (scope.path, scope.query, scope.fragment)
                        if scope.query or scope.fragment else
                        target.path == scope.path.rstrip('/') or target.path.startswith(scope.path.rstrip('/') + '/'))
                       for target in targets):
                    names.add(path['unit_name'])
                    break
        return names
