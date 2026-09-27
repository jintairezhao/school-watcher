"""Compare discovery against independently reviewed, versioned official rosters."""
import hashlib
import json

from bs4 import BeautifulSoup

from .source_inventory import canonical_url, now, site_key


def publication_hash(feed_json):
    evidence = json.loads(feed_json) if feed_json else None
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check_baseline(inventory, baseline):
    key = site_key(baseline['root_url'])
    reference = canonical_url(baseline['reference_url'])
    html = inventory.snapshot(key, reference)
    entries = baseline['entries']
    if not entries or len({e['id'] for e in entries}) != len(entries):
        raise ValueError('A reviewed baseline needs nonempty, unique entry identities')
    actual_hash = hashlib.sha256(html.encode()).hexdigest() if html else None
    fresh = actual_hash == baseline['reference_hash']
    result = {'id': baseline['id'], 'scope': baseline['scope'], 'category': baseline['category'],
              'reference_url': reference, 'reference_hash': actual_hash,
              'reviewed_at': baseline['reviewed_at'], 'checked_at': now(),
              'reference_current': fresh, 'total': len(entries), 'matched': 0,
              'entries': [], 'unexpected': [], 'scope_passed': False}
    if not fresh:
        result['status'] = 'reference_changed' if html else 'reference_missing'
        return result
    page = inventory.get_page(key, reference)
    if page is None or page['state'] != 'fetched' or page['content_hash'] != actual_hash:
        result.update(reference_current=False, status='reference_unavailable')
        return result

    if baseline['category'] == 'publication_columns':
        return check_publication_columns(inventory, key, baseline, html, result)

    soup = BeautifulSoup(html, 'lxml')
    scopes = soup.select(baseline['scope_selector'])
    if not scopes:
        result['status'] = 'scope_missing'
        return result
    nodes = []
    all_nodes = [n for n in inventory.structure(key) if n['reference_url'] == reference]
    owners = {n['node_key'] for n in all_nodes if n['relation'] == 'page_identity'}
    for node in all_nodes:
        if node['locator'] == 'document':
            continue
        element = soup.select_one(node['locator'].split('::text(')[0])
        if element is not None and any(element is s or any(p is s for p in element.parents) for s in scopes):
            nodes.append(node)
    expected = {e['id']: e for e in entries}
    programme_attributes = {}
    for note in json.loads(page['notes_json'] or '[]'):
        if note.startswith('programme_catalog_attributes:'):
            programme_attributes.update({e['key']: e['fields'] for e in json.loads(note.split(':', 1)[1])})
    resolved, resolving = {}, set()

    def match(entry):
        entry_id = entry['id']
        if entry_id in resolved:
            return resolved[entry_id]
        if entry_id in resolving:
            raise ValueError('Baseline parent relationship contains a cycle')
        resolving.add(entry_id)
        parent = entry.get('parent')
        parent_keys = owners
        if parent:
            if parent not in expected:
                raise ValueError('Baseline references an unknown parent')
            parent_keys = set(match(expected[parent])['keys'])
        names = [n for n in nodes if n['name'] == entry['name'] and n['kind'] == entry.get('kind', 'unit')]
        urls = [n for n in names if canonical_url(n['url']) == canonical_url(entry.get('url', ''))]
        placements = [n for n in urls if n['parent_key'] in parent_keys]
        relations = [n for n in placements if not entry.get('relation') or n['relation'] == entry['relation']]
        matching_attributes = [n for n in relations if all(programme_attributes.get(n['node_key'], {}).get(k) == v
                               for k, v in entry.get('fields', {}).items())]
        keys = sorted({n['node_key'] for n in matching_attributes})
        status = ('matched' if len(keys) == 1 else 'ambiguous' if keys else
                  'wrong_attributes' if relations else
                  'wrong_relation' if placements else
                  'wrong_parent' if urls else 'wrong_url' if names else 'missing')
        item = {'id': entry_id, 'name': entry['name'], 'parent': parent, 'status': status,
                'keys': keys, 'locators': sorted({n['locator'] for n in relations})}
        resolved[entry_id] = item
        resolving.remove(entry_id)
        return item

    result['entries'] = [match(e) for e in entries]
    result['matched'] = sum(e['status'] == 'matched' for e in result['entries'])
    matched_keys = {k for e in result['entries'] if e['status'] == 'matched' for k in e['keys']}
    unexpected = {n['node_key']: {'name': n['name'], 'kind': n['kind'], 'url': n['url'],
                                'locator': n['locator']} for n in nodes if n['node_key'] not in matched_keys}
    result['unexpected'] = list(unexpected.values())
    result['scope_passed'] = result['matched'] == result['total'] and not unexpected
    result['status'] = 'scope_matched' if result['scope_passed'] else 'mismatch'
    return result


def check_publication_columns(inventory, key, baseline, html, result):
    """Compare named widgets and all their article URLs with an independent reviewed list."""
    soup = BeautifulSoup(html, 'lxml')
    report = inventory.report(key)
    page = next((p for p in report['pages'] if p['url'] == baseline['reference_url']), None)
    evidence = json.loads(page['feed_json']) if page and page.get('feed_json') else {}
    result['feed_hash'] = publication_hash(page.get('feed_json') if page else None)
    feeds = evidence.get('lists', [evidence] if evidence else [])
    if baseline.get('scope_selector'):
        scopes = soup.select(baseline['scope_selector'])
        if not scopes:
            result['status'] = 'scope_missing'
            return result
        # Keep intersecting lists, including a wrongly oversized list that spans
        # this scope, so a partial check cannot hide a cross-column extraction.
        feeds = [feed for feed in feeds if any(
            item is scope or any(p is scope for p in item.parents) or any(p is item for p in scope.parents)
            for item in soup.select(feed['list_selector']) for scope in scopes)]
    claimed = set()
    for entry in baseline['entries']:
        scope = soup.select_one(entry['scope_selector'])
        matches = []
        for i, feed in enumerate(feeds):
            if feed.get('name') != entry['name'] or canonical_url(feed.get('column_url', '')) != canonical_url(entry['url']):
                continue
            if entry.get('group_name') and feed.get('column_group_name') != entry['group_name']:
                continue
            items = soup.select(feed['list_selector'])
            inside = scope is not None and items and all(
                item is scope or any(p is scope for p in item.parents) for item in items)
            if inside:
                matches.append((i, feed))
        status = 'missing_or_wrong_scope'
        if len(matches) == 1:
            index, feed = matches[0]
            expected_urls = {canonical_url(u) for u in entry['article_urls']}
            observed_urls = {canonical_url(u) for u in feed.get('article_urls', [])}
            status = 'matched' if observed_urls == expected_urls else 'article_range_mismatch'
            if status == 'matched':
                claimed.add(index)
        elif matches:
            status = 'ambiguous'
        result['entries'].append({'id': entry['id'], 'name': entry['name'], 'status': status})
    result['matched'] = sum(e['status'] == 'matched' for e in result['entries'])
    result['unexpected'] = [{'name': f.get('name') or '未命名发布区域', 'url': f.get('column_url', '')}
                            for i, f in enumerate(feeds) if i not in claimed]
    result['scope_passed'] = result['matched'] == result['total'] and not result['unexpected']
    result['status'] = 'scope_matched' if result['scope_passed'] else 'mismatch'
    return result


def save_baseline_check(inventory, baseline):
    result = check_baseline(inventory, baseline)
    key = site_key(baseline['root_url'])
    with inventory.connect() as c:
        c.execute('INSERT INTO reference_checks VALUES(?,?,?,?,?) '
                  'ON CONFLICT(site_key,baseline_id) DO UPDATE SET '
                  'baseline_json=excluded.baseline_json,result_json=excluded.result_json,checked_at=excluded.checked_at',
                  (key, baseline['id'], json.dumps(baseline, ensure_ascii=False),
                   json.dumps(result, ensure_ascii=False), now()))
    return result
