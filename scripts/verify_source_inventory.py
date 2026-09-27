"""Reparse stored evidence and check reviewed roster scopes without network requests."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from filelock import FileLock, Timeout
from backend.services.source_inventory import Inventory, DEFAULT_PATH, site_key
from backend.services.source_baselines import save_baseline_check
from backend.scraper.discovery.inventory_crawler import inspect_page
from scripts.reparse_sources import cached_response


def verify_file(inventory, path, reparse=False):
    baseline = json.loads(path.read_text(encoding='utf-8'))
    key = site_key(baseline['root_url'])
    with FileLock(str(inventory.path.parent / ('source-crawl-' + key + '.lock')), timeout=0):
        if reparse:
            url = baseline['reference_url']
            report = inventory.report(key)
            page = next((p for p in report['pages'] if p['url'] == url), None) if report else None
            html = inventory.snapshot(key, url)
            if page and html and page['state'] == 'fetched':
                response = cached_response(page, html)
                inspect_page(inventory, report['site'], page, fetcher=lambda _: response)
        return save_baseline_check(inventory, baseline)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baselines', type=Path, nargs='*')
    parser.add_argument('--inventory', type=Path, default=DEFAULT_PATH)
    parser.add_argument('--reparse', action='store_true')
    parser.add_argument('--details', action='store_true')
    args = parser.parse_args()
    inventory = Inventory(args.inventory)
    paths = args.baselines or sorted((ROOT / 'docs' / 'source_baselines').glob('*.json'))
    failed, busy = False, False
    for path in paths:
        try:
            result = verify_file(inventory, path, args.reparse)
        except Timeout:
            busy = True
            print(json.dumps({'baseline': path.name, 'state': 'already_running'}))
            continue
        failed |= not result['scope_passed']
        output = result if args.details else {k: v for k, v in result.items() if k != 'entries'}
        print(json.dumps(output, ensure_ascii=True))
    sys.exit(1 if failed else 2 if busy else 0)
