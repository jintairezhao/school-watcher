"""Replay retained real navigation, with no network, model or source installs.

This compares exploration order only. It cannot establish integration success,
provider cost savings or fresh-site coverage. Input is opened query-only and
backed up into distinct working copies; outgoing edges appear only after the
corresponding originally complete page has been selected.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def forbid_network(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo'):
        raise RuntimeError('Retained navigation comparison forbids network access')


def replay(source, directory, site_key, steps, chooser=None):
    from backend.services.source_inventory import Inventory
    from backend.scraper.discovery.attention import complete_material
    from backend.scraper.discovery import frontier
    path = directory / 'inventory.sqlite3'
    with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as original:
        original.execute('PRAGMA query_only=ON')
        with sqlite3.connect(path) as target:
            original.backup(target)
    inventory = Inventory(path)
    with inventory.connect() as c:
        site = dict(c.execute('SELECT * FROM sites WHERE site_key=?', (site_key,)).fetchone())
        originals = {row['url']: dict(row) for row in c.execute(
            'SELECT * FROM pages WHERE site_key=?', (site_key,))}
        c.execute('DELETE FROM frontier_choices WHERE site_key=?', (site_key,))
        c.execute("UPDATE pages SET state='not_observed',attempts=0,checked_at=NULL,retry_at=NULL WHERE site_key=?", (site_key,))
        c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=?", (site_key, site['root_url']))
    current = frontier.choose_route
    if chooser is not None:
        allowed = inspect.signature(chooser).parameters
        frontier.choose_route = lambda pages, **kwargs: chooser(pages, **{
            key: value for key, value in kwargs.items() if key in allowed})
    trace = []
    try:
        for turn in range(steps):
            page = inventory.claim(site_key, focus='valuable')
            if page is None:
                break
            observed = originals[page['url']]
            available = complete_material(observed)
            trace.append({'claim': turn + 1, 'url': page['url'], 'label': page['label'],
                          'kind': page['kind'], 'retained_complete_material': available})
            with inventory.connect() as c:
                c.execute('UPDATE pages SET state=?,checked_at=? WHERE site_key=? AND url=?',
                    ('fetched' if available else 'reference_only', observed.get('checked_at'), site_key, page['url']))
                if available:
                    c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND state='not_observed' "
                        "AND url IN (SELECT target_url FROM edges WHERE site_key=? AND parent_url=? "
                        "AND content_hash=? AND decision IN ('follow','official_external_link'))",
                        (site_key, site_key, page['url'], observed['content_hash']))
    finally:
        frontier.choose_route = current
    # No SQLAlchemy app is created: paid execution and source installation are
    # deliberately impossible. Retained source outcomes are not invented.
    diagnostics = inventory.exploration_diagnostics(site_key)
    return {'trace': trace, 'diagnostics': diagnostics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--site-key')
    parser.add_argument('--steps', type=int, default=60)
    parser.add_argument('--profiles', nargs='+')
    parser.add_argument('--baseline-ref', default='HEAD')
    args = parser.parse_args()
    if args.steps < 1:
        parser.error('--steps must be positive; it is a replay window, not a discovery limit')
    source, output = args.inventory.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    before_hash = fingerprint(source)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as c:
        rows = c.execute('SELECT site_key FROM sites ORDER BY site_key').fetchall()
    if not args.site_key and len(rows) != 1:
        parser.error('--site-key is required for an inventory containing multiple schools')
    site_key = args.site_key or rows[0][0]
    if args.profiles:
        os.environ['WATCHER_DISCOVERY_ATTENTION'] = json.dumps({'profiles': args.profiles})
    baseline_text = subprocess.run(['git', 'show', args.baseline_ref +
        ':backend/scraper/discovery/frontier.py'], cwd=ROOT, check=True,
        capture_output=True, text=True, encoding='utf-8').stdout
    namespace = {'__name__': 'backend.scraper.discovery.retained_baseline',
                 '__package__': 'backend.scraper.discovery'}
    exec(compile(baseline_text, 'retained-baseline-frontier', 'exec'), namespace)
    sys.addaudithook(forbid_network)
    result = {'kind': 'retained_navigation_order_comparison',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'baseline_ref': args.baseline_ref, 'baseline_sha256': hashlib.sha256(baseline_text.encode()).hexdigest(),
        'actual_model_requests': 0, 'actual_website_reads': 0,
        'source_installs_measured': False, 'actual_token_savings_measured': False,
        'limits': ['Retained classifications and graph, without new model decisions.',
                   'Queue steps do not measure real latency, retries or provider cost.',
                   'Unread real pages remain unknown leaves.',
                   'Observed demand cells have no known whole-school denominator.']}
    for name, chooser in (('before', namespace['choose_route']), ('after', None)):
        directory = output / name
        directory.mkdir()
        result[name] = replay(source, directory, site_key, args.steps, chooser)
    result['input_sha256_before'] = before_hash
    result['input_sha256_after'] = fingerprint(source)
    result['input_unchanged'] = result['input_sha256_before'] == result['input_sha256_after']
    (output / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    if not result['input_unchanged']:
        raise RuntimeError('Input changed during read-only comparison; inspect concurrent writers')
    print(json.dumps({'result': str(output / 'results.json'), 'input_unchanged': True,
        'before_steps': len(result['before']['trace']), 'after_steps': len(result['after']['trace']),
        'actual_model_requests': 0, 'actual_website_reads': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
