"""Give every school a bounded first read of known, unread student-source entrances."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from filelock import FileLock, Timeout
from backend.services.source_inventory import Inventory, DEFAULT_PATH
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.scraper.discovery.parser_revision import assert_current_parser
from backend.scraper.discovery.structure import classify
from scripts.sources.audit_student_sources import audit, CATEGORIES


def choose_entrances(school):
    """One first-party pending entrance per unread category; failures need separate review."""
    selected = {}
    for category in CATEGORIES:
        scope = school['student_sources'][category]
        pending = [p for p in scope['leads'] if p['state'] in ('pending', 'not_queued')]
        primary = [p for p in pending if school['root_url'] in p.get('reference_urls', [])]
        if scope['status'] != 'located_not_read':
            # Reading one college's introduction is not a reason to leave the
            # university homepage's own programme/academic entrance unread.
            pending = primary
        if pending:
            page = min(primary or pending, key=lambda p: (p['url'].count('/'), len(p['url']), p['url']))
            selected.setdefault(page['url'], page)
    return list(selected.values())


def read_school(inventory, school):
    assert_current_parser()
    key = school['site_key']
    targets = choose_entrances(school)
    if not targets:
        return {'school': school['name'], 'results': []}
    try:
        with FileLock(str(inventory.path.parent / ('source-crawl-' + key + '.lock')), timeout=0):
            results = []
            for lead in targets:
                assert_current_parser()
                # Recheck the live state: the audit may have preceded another task.
                with inventory.connect() as connection:
                    row = connection.execute('SELECT * FROM pages WHERE site_key=? AND url=?',
                                             (key, lead['url'])).fetchone()
                if row and row['state'] != 'pending':
                    continue
                if row is None:
                    inventory.enqueue(key, lead['url'], lead['label'], classify(lead['label']) or 'navigation',
                                      1, [school['name']], 'school_domain')
                    with inventory.connect() as connection:
                        row = connection.execute('SELECT * FROM pages WHERE site_key=? AND url=?',
                                                 (key, lead['url'])).fetchone()
                inspect_page(inventory, school, dict(row))
                with inventory.connect() as connection:
                    current = dict(connection.execute(
                        'SELECT url,label,state,status_code,final_url,title,error,checked_at FROM pages '
                        'WHERE site_key=? AND url=?', (key, lead['url'])).fetchone())
                results.append(current)
            return {'school': school['name'], 'results': results}
    except Timeout:
        return {'school': school['name'], 'state': 'already_running', 'results': []}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, default=DEFAULT_PATH)
    parser.add_argument('--school-workers', type=int, choices=range(1, 5), default=3)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/source-audits/student-entrance-first-reads.json')
    args = parser.parse_args()
    inventory = Inventory(args.inventory)
    before = audit(inventory)
    schools = [s for s in before['schools'] if choose_entrances(s)]
    print(json.dumps({'school_count': len(schools), 'planned_entrances': sum(len(choose_entrances(s)) for s in schools)}), flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.school_workers) as pool:
        for result in pool.map(lambda s: read_school(inventory, s), schools):
            results.append(result)
            print(json.dumps(result, ensure_ascii=True), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'audit_at': before['generated_at'], 'results': results,
                                      'coverage_accepted': False}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
