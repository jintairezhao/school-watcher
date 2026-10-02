"""Durable structural baselines: publishing new articles is not a site redesign."""
import hashlib
import json
import re

from bs4 import BeautifulSoup

PREFIX = 'discovery_shape:'
VERSION = '1'


def baseline(page):
    for note in json.loads((page or {}).get('notes_json') or '[]'):
        if isinstance(note, str) and note.startswith(PREFIX):
            return json.loads(note[len(PREFIX):])
    return {}


def put(notes, value):
    return [n for n in notes if not n.startswith(PREFIX)] + [PREFIX + json.dumps(value, ensure_ascii=False)]


def fingerprint(parsed, feed, html, final_url):
    # Retain navigation names/targets and DOM templates, never article text,
    # dates, article addresses, list length, rotating images or script payloads.
    links = sorted({(l['url'], l['label'], l['kind']) for l in parsed['links']
                    if l['kind'] != 'article' and l['decision'] in
                    ('follow', 'official_external_link', 'navigation_pending')})
    soup = BeautifulSoup(html, 'lxml')
    regions = soup.select('nav, [role=navigation], main, article') or [soup.body or soup]
    shapes = set()
    for region in regions:
        for tag in region.find_all(True):
            if tag.name in ('script', 'style', 'img', 'svg', 'path', 'iframe'):
                continue
            chain = [tag, *list(tag.parents)[:4]]
            shapes.add('/'.join(n.name + '#' + re.sub(r'\d+', '*', str(n.get('id', ''))) + '.' +
                               re.sub(r'\d+', '*', '.'.join(sorted(n.get('class', [])))) for n in chain if n.name))
    fields = ('name', 'list_selector', 'title_selector', 'link_selector', 'date_selector',
              'column_url', 'container_locator', 'heading_locator')
    columns = [{k: col.get(k) for k in fields} for col in (feed.get('lists', [feed]) if feed else [])]
    value = [VERSION, final_url, links, sorted(shapes), sorted(columns, key=lambda c: str(c))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def compare(page, parsed, feed, html, final_url):
    from backend.services.tasks import current_execution
    handle = current_execution() or {}
    old = baseline(page)
    shape = fingerprint(parsed, feed, html, final_url)
    state = 'unchanged' if old.get('hash') == shape else 'changed' if old else 'new'
    value = {'version': VERSION, 'hash': shape, 'change': state,
             'run': f"{handle.get('id', 0)}:{handle.get('generation', 0)}",
             'navigation_complete': old.get('navigation_complete', False) if state == 'unchanged' else False,
             'routes': old.get('routes', []) if state == 'unchanged' else []}
    # Replay only links still present in this document. Saved AI decisions are
    # not permission to invent or revisit a removed address.
    if state == 'unchanged':
        for link in parsed['links']:
            saved = next((r for r in value['routes'] if r['url'] == link['url'] and r['label'] == link['label']), None)
            if saved:
                link.update(kind=saved['kind'], decision=saved['decision'])
    return value


def counts(report):
    from backend.services.tasks import current_execution
    handle = current_execution() or {}
    run_key = f"{handle.get('id', 0)}:{handle.get('generation', 0)}"
    values = [baseline(p) for p in report['pages'] if p.get('state') == 'fetched']
    return {state: sum(v.get('run') == run_key and v.get('change') == state for v in values)
            for state in ('new', 'changed', 'unchanged')}


def remember_navigation(inventory, key, url, parsed, complete):
    page = inventory.get_page(key, url)
    if not page:
        return
    saved = baseline(page)
    if not saved:
        return
    saved['navigation_complete'] = complete
    saved['routes'] = [{k: l[k] for k in ('url', 'label', 'kind', 'decision')}
                       for l in parsed['links'] if l['decision'] in ('follow', 'official_external_link')]
    with inventory.connect() as c:
        c.execute('UPDATE pages SET notes_json=? WHERE site_key=? AND url=?',
                  (json.dumps(put(json.loads(page['notes_json']), saved), ensure_ascii=False), key, url))


def seed_check(inventory, key, catalog, run_key, extra_pages=()):
    """Hydrate baselines after scratch eviction; probe known entrances once/run.

    Probing every known department is necessary: its template can change while
    the school homepage stays identical. No article/history pages are seeded.
    """
    report = catalog.report(key) or {}
    with inventory.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS change_runs (site_key TEXT PRIMARY KEY, run_key TEXT)')
        prior = c.execute('SELECT run_key FROM change_runs WHERE site_key=?', (key,)).fetchone()
        if prior and prior[0] == run_key:
            return
    pages = {p['url']: p for p in extra_pages}
    pages.update({p['url']: p for p in report.get('pages', [])})
    for page in pages.values():
        if page['kind'] not in ('root', 'directory', 'unit', 'gateway', 'channel') and not page.get('feed_json'):
            continue
        inventory.enqueue(key, page['url'], page['label'], page['kind'], page['depth'],
                          json.loads(page['path_json']), page['authority'])
        with inventory.connect() as c:
            # Preserve unfinished traversal. Missing metadata is recovered from
            # the published catalogue, which is not subject to scratch cleanup.
            c.execute("UPDATE pages SET notes_json=CASE WHEN content_hash IS NULL THEN ? ELSE notes_json END, "
                      "feed_json=coalesce(feed_json,?), state=CASE WHEN state='running' THEN state ELSE 'pending' END "
                      'WHERE site_key=? AND url=?', (page.get('notes_json') or '[]', page.get('feed_json'), key, page['url']))
    with inventory.connect() as c:
        c.execute('INSERT OR REPLACE INTO change_runs VALUES (?,?)', (key, run_key))


def ensure_initial(school):
    """Subscription can bootstrap discovery once; resubscribe is not a rerun."""
    from backend.database.db import db
    from backend.database.dialect import insert
    from backend.database.models import BackgroundTask, AppConfig
    from backend.services import tasks
    marker = 'discovery_started_' + str(school.id)
    existing = BackgroundTask.query.filter_by(identity=f'discover:{school.id}').first()
    if AppConfig.get(marker):
        return existing
    job = existing or tasks.enqueue('discover', school.id, {'school_id': school.id, 'ai_assist': True,
                        'trigger': 'first_subscription'}, replace_finished=False, expedite=True)
    # Concurrent first subscribers share both the task and its durable marker.
    db.session.execute(insert(AppConfig).values(key=marker, value='1')
                       .on_conflict_do_nothing(index_elements=['key']))
    db.session.commit()
    return job
