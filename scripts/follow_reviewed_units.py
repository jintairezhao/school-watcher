"""Read unvisited unit entrances from a currently matching independent official roster."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock
from backend.services.source_inventory import Inventory, DEFAULT_PATH, site_key
from backend.services.source_baselines import check_baseline
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.scraper.discovery.parser_revision import assert_current_parser


def follow(inventory, baseline, workers=2):
    assert_current_parser()
    if baseline['category'] not in ('academic_units', 'institutional_units', 'administrative_units'):
        raise ValueError('Only an independently reviewed institutional roster can seed unit entrances.')
    key = site_key(baseline['root_url'])
    with FileLock(str(inventory.path.parent / ('source-crawl-' + key + '.lock')), timeout=0):
        check = check_baseline(inventory, baseline)
        if not check['scope_passed']:
            raise ValueError('The reviewed roster no longer matches its current evidence: ' + check['status'])
        report = inventory.report(key)
        reference = next(p for p in report['pages'] if p['url'] == baseline['reference_url'])
        for entry in baseline['entries']:
            if (entry.get('kind', 'unit') != 'unit' or not entry.get('url')
                    or entry.get('relation') in ('hidden_directory_entry', 'table_reference')):
                continue
            inventory.enqueue(key, entry['url'], entry['name'], 'unit', reference['depth'] + 1,
                              [reference['label']], 'official_backlink')
        targets = {e['url'] for e in baseline['entries'] if e.get('kind', 'unit') == 'unit'
                   and e.get('url') and e.get('relation') not in ('hidden_directory_entry', 'table_reference')}
        report = inventory.report(key)
        pages = [p for p in report['pages'] if p['url'] in targets and p['state'] == 'pending']
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(inspect_page, inventory, report['site'], page): page for page in pages}
            for future in as_completed(futures):
                future.result()
                page = futures[future]
                with inventory.connect() as connection:
                    current = dict(connection.execute('SELECT state,final_url,title,error FROM pages WHERE site_key=? AND url=?',
                                                      (key, page['url'])).fetchone())
                print(json.dumps({'school': report['site']['name'], 'unit': page['label'], 'url': page['url'],
                                  **current}, ensure_ascii=True), flush=True)
    return {'baseline': baseline['id'], 'reviewed_entries': len(targets), 'processed': len(pages)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baselines', nargs='+', type=Path)
    parser.add_argument('--inventory', type=Path, default=DEFAULT_PATH)
    parser.add_argument('--workers', type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    inventory = Inventory(args.inventory)
    for path in args.baselines:
        print(json.dumps(follow(inventory, json.loads(path.read_text(encoding='utf-8')), args.workers), ensure_ascii=True), flush=True)
