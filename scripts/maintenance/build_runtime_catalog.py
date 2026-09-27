"""Project a legacy investigation database into a checked, small runtime catalogue."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.services.source_inventory import Inventory, site_key
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.source_baselines import BASELINE_DIRECTORY, check_baseline


def build(source, output, audit_path):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or not source.is_file():
        raise ValueError('A separate existing source and new catalogue path are required')
    # Do not call the scratch database constructor or change source schema.
    inventory = Inventory.__new__(Inventory)
    inventory.path = source
    catalog = RuntimeCatalog(output)
    baselines = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(BASELINE_DIRECTORY.glob('*.json'))]
    before = [check_baseline(inventory, b) for b in baselines]
    failures = [r['id'] for r in before if not r['scope_passed']]
    if failures:
        raise RuntimeError('Source baselines must pass before export: ' + ', '.join(failures))
    sites = inventory.all_sites()
    completed = {s['site_key'] for s in catalog.all_sites()}
    for index, site in enumerate(sites, 1):
        if site['site_key'] not in completed:
            catalog.publish(inventory, site['site_key'])
        print(json.dumps({'school': site['name'], 'completed': index, 'total': len(sites)}, ensure_ascii=False), flush=True)
    after = [check_baseline(catalog, b) for b in baselines]
    failed = [r['id'] for r in after if not r['scope_passed']]
    if failed:
        raise RuntimeError('Catalogue validation failed: ' + ', '.join(failed))
    with catalog.connect(write=True) as connection:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.execute('VACUUM')
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Catalogue integrity check failed')
    audit = {'source_bytes': source.stat().st_size, 'catalogue_bytes': output.stat().st_size,
             'school_count': len(sites), 'baseline_count': len(after),
             'baselines_passed': all(r['scope_passed'] for r in after),
             'global_coverage_accepted': False,
             'checks': [{k: r[k] for k in ('id', 'matched', 'total', 'scope_passed')} for r in after]}
    Path(audit_path).write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in audit.items() if k != 'checks'}), flush=True)
    return audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=ROOT / 'data/source_inventory.sqlite3')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/source_catalog.build.sqlite3')
    parser.add_argument('--audit', type=Path, default=ROOT / 'data/source-audits/runtime-catalogue-migration.json')
    args = parser.parse_args()
    build(args.source, args.output, args.audit)
