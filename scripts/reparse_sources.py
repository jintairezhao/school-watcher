"""Apply improved parsing to stored official snapshots without claiming a new web visit."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from filelock import FileLock, Timeout
from backend.services.source_inventory import Inventory, DEFAULT_PATH, site_key
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.scraper.discovery.parser_revision import PARSER_REVISION, assert_current_parser


def cached_response(page, html):
    return {'html': html, 'url': page['final_url'] or page['url'],
            'status': page['status_code'], 'checked_at': page['checked_at'],
            'network_notes': [n for n in json.loads(page['notes_json']) if n.startswith(
                ('public_dns_', 'direct_after_proxy_error:', 'repaired_same_host_https_redirect:'))]}


def reparse_site(inventory, key, force=False, roots_only=False):
    assert_current_parser()
    with FileLock(str(inventory.path.parent / ('source-crawl-' + key + '.lock')), timeout=0):
        report = inventory.report(key)
        processed = 0
        current = 0
        for page in report['pages']:
            if page['state'] != 'fetched':
                continue
            if roots_only and page['kind'] != 'root':
                continue
            if not force and page.get('parser_revision') == PARSER_REVISION:
                current += 1
                continue
            html = inventory.snapshot(key, page['url'])
            if not html:
                continue
            response = cached_response(page, html)
            inspect_page(inventory, report['site'], page, fetcher=lambda _url: response)
            processed += 1
        assert_current_parser()
        referenced = inventory.reconcile_article_references(key)
        inventory.settle(key)
        return {'site_key': key, 'reparsed': processed, 'already_current': current,
                'reclassified_article_references': referenced, 'parser_revision': PARSER_REVISION}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', action='append', default=[])
    parser.add_argument('--inventory', type=Path, default=DEFAULT_PATH)
    parser.add_argument('--force', action='store_true', help='Reparse even pages already using this parser revision')
    parser.add_argument('--roots-only', action='store_true', help='Update school homepages first; retain every remaining page')
    args = parser.parse_args()
    inventory = Inventory(args.inventory)
    keys = [site_key(url) for url in args.url] or [s['site_key'] for s in inventory.all_sites()]
    for key in keys:
        try:
            print(json.dumps(reparse_site(inventory, key, args.force, args.roots_only)), flush=True)
        except Timeout:
            print(json.dumps({'site_key': key, 'state': 'already_running'}), flush=True)
