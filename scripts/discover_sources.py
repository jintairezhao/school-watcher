"""Audit source structure without modifying user subscriptions or announcements."""
import argparse
from contextlib import closing, nullcontext
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import threading
from filelock import FileLock, Timeout

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.source_inventory import Inventory, DEFAULT_PATH
from backend.scraper.discovery.inventory_crawler import crawl_site
from backend.services.source_catalog import publication_candidates


def load_round(path, keys, focus, max_pages):
    specification = {'site_keys': sorted(keys), 'focus': focus, 'max_pages': max_pages}
    if path.exists():
        state = json.loads(path.read_text(encoding='utf-8'))
        if state.get('specification') != specification:
            raise ValueError('Checkpoint belongs to a different school set or crawl slice; use a new checkpoint file.')
        if not set(state.get('completed_slices', {})) <= set(keys):
            raise ValueError('Checkpoint contains unknown schools.')
        return state
    return {'specification': specification, 'completed_slices': {}, 'coverage_accepted': False}


def save_round(path, state):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name')
    parser.add_argument('--url')
    parser.add_argument('--registered', action='store_true', help='Prioritize schools already added by users')
    parser.add_argument('--catalog', action='store_true', help='Include every school in the built-in directory')
    parser.add_argument('--max-pages', type=int, default=250, help='Per-school slice; pending pages are retained')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--school-workers', type=int, default=1,
                        help='Independent schools checked concurrently (1-4); per-host pacing still applies')
    parser.add_argument('--inventory', default=str(DEFAULT_PATH))
    parser.add_argument('--report-only', action='store_true')
    parser.add_argument('--checkpoint', type=Path,
                        help='Resume this bounded all-school round after a restart; completed slices are not coverage approval')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--focus', choices=('student', 'all'), default='student',
                        help='Prioritize departments, majors and teaching notices; retain other entrances')
    args = parser.parse_args()
    if args.checkpoint and args.report_only:
        parser.error('--checkpoint requires a crawl, not --report-only')
    inventory = Inventory(args.inventory)
    sites = []
    if args.registered:
        with closing(sqlite3.connect((ROOT / 'data' / 'school_watcher.db').as_uri() + '?mode=ro', uri=True)) as c:
            sites.extend(c.execute('SELECT name,url FROM schools WHERE enabled=1 '
                                   'ORDER BY subscriber_count DESC,id').fetchall())
    if args.catalog:
        from backend.services.catalog import catalog_entries
        sites.extend((e['name'], e['url']) for e in catalog_entries())
    if args.name and args.url:
        sites.insert(0, (args.name, args.url))
    keys = list(dict.fromkeys(inventory.ensure_site(name, url) for name, url in sites))
    if not keys:
        keys = [s['site_key'] for s in inventory.all_sites()]
    output_lock = threading.Lock()
    round_state = None

    def emit(value):
        with output_lock:
            print(json.dumps(value, ensure_ascii=True), flush=True)

    def audit_site(key):
        if args.report_only:
            result = inventory.report(key)
        else:
            def progress(n, report):
                if n % 20 == 0:
                    emit({'school': report['site']['name'], 'processed': n, 'states': report['states']})
            try:
                result = crawl_site(inventory, key, max_pages=max(1, args.max_pages),
                                    workers=max(1, min(8, args.workers)), progress=progress,
                                    retry_failed=args.retry_failed, focus=args.focus)
            except Timeout:
                emit({'site_key': key, 'state': 'already_running'})
                return
        current_pages = {p['url']: p for p in result['pages'] if p['state'] == 'fetched'}
        structure = inventory.structure(key)
        emit({'school': result['site']['name'], 'focus': args.focus, 'states': result['states'],
                          'pages': len(result['pages']), 'edges': result['edge_count'],
                          'observed_unit_identities': len({n['node_key'] for n in structure if n['kind'] == 'unit'
                              and n['relation'] != 'page_identity' and n['reference_url'] in current_pages
                              and n['content_hash'] == current_pages[n['reference_url']]['content_hash']}),
                          'candidate_feeds': len(publication_candidates(result, structure, focus=args.focus)),
              'metrics': result['metrics'], 'accepted': result['accepted']})
        if round_state is not None:
            from backend.services.source_inventory import now
            from backend.scraper.discovery.parser_revision import PARSER_REVISION
            with output_lock:
                round_state['completed_slices'][key] = {
                    'school': result['site']['name'], 'finished_at': now(),
                    'processed_this_run': result['processed_this_run'], 'parser_revision': PARSER_REVISION}
                save_round(args.checkpoint, round_state)

    if args.checkpoint:
        args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(args.checkpoint) + '.lock', timeout=0) if args.checkpoint else nullcontext():
        if args.checkpoint:
            round_state = load_round(args.checkpoint, keys, args.focus, max(1, args.max_pages))
            save_round(args.checkpoint, round_state)
            keys = [key for key in keys if key not in round_state['completed_slices']]
            emit({'remaining_school_slices': len(keys), 'completed_school_slices': len(round_state['completed_slices']),
                  'coverage_accepted': False})
        with ThreadPoolExecutor(max_workers=max(1, min(4, args.school_workers))) as pool:
            for _ in pool.map(audit_site, keys):
                pass


if __name__ == '__main__':
    main()
