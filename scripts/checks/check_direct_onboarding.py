"""Exercise real discover/onboard workers against an official site in temporary storage.

No user database, stored provider key, or paid model is used. Reports the actual
department/column tree after a bounded sample; this does not certify coverage.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def run(args):
    with tempfile.TemporaryDirectory(prefix='school-onboarding-') as folder:
        root = Path(folder)
        os.environ['WATCHER_DATA_DIR'] = str(root)
        os.environ['WATCHER_ENV_FILE'] = str(root / '.env')
        os.environ['WATCHER_SEED_ON_START'] = '0'
        os.environ['WATCHER_BROWSER'] = '0'
        from backend import create_app
        from backend.ai.configuration import AIConfigError
        from backend.database.db import db
        from backend.database.models import Announcement, BackgroundTask, Department, School
        from backend.database.source_governance_models import SourceProposal
        from backend.services import tasks
        from backend.services.discovery_cache import DiscoveryCache
        from backend.services.source_inventory import site_key
        from backend.worker import dispatch

        app = create_app({'TESTING': True, 'SECRET_KEY': 'isolated-onboarding-check',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(root / 'test.db'),
            'SOURCE_CATALOG_PATH': str(root / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(root / 'discovery.db'),
            'SOURCE_GOVERNANCE_EVIDENCE_PATH': str(root / 'evidence')})
        report = {'school': args.school, 'root_url': args.url, 'state': 'sample_incomplete',
                  'steps': [], 'ai_calls': 0, 'whole_school_verified': False}
        started = time.monotonic()
        with app.app_context(), patch('backend.ai.configuration.get_model_binding',
                side_effect=AIConfigError('AI disabled in isolated acceptance check')), \
                patch('backend.ai.providers.complete', side_effect=AssertionError('Paid AI must not run')) as provider:
            db.create_all()
            school = School(name=args.school, url=args.url)
            db.session.add(school); db.session.commit()
            tasks.enqueue('discover', school.id, {'school_id': school.id, 'ai_assist': False})
            inventory = DiscoveryCache(app.config['DISCOVERY_CACHE_PATH'])
            key = site_key(args.url)
            try:
                while time.monotonic() - started < args.max_seconds:
                    snapshot = inventory.progress_snapshot(key) or {}
                    states = snapshot.get('states', {})
                    checked = sum(n for state, n in states.items() if state not in ('pending', 'running'))
                    # Give already discovered columns their independent turn.
                    handle = tasks.claim(capabilities=['http'])
                    if handle is None and checked < args.max_pages:
                        handle = tasks.claim(capabilities=['directory'])
                    if handle is None:
                        pending = BackgroundTask.query.filter_by(state='pending').count()
                        if not pending or checked >= args.max_pages:
                            break
                        db.session.commit()
                        time.sleep(0.5)
                        continue
                    step = {'kind': handle['kind']}
                    try:
                        with tasks.execution_scope(handle):
                            result = dispatch(handle['kind'], handle['payload'])
                        tasks.finish(handle, result)
                        step['state'] = result.get('state', 'done')
                    except tasks.TaskDeferred as deferred:
                        db.session.rollback()
                        tasks.handoff(handle, deferred)
                        step.update(state='waiting', phase=deferred.phase)
                    except Exception as exc:
                        db.session.rollback()
                        tasks.finish(handle, error=exc)
                        step.update(state='failed', error_type=type(exc).__name__, error=str(exc)[:250])
                    report['steps'].append(step)
                    print(json.dumps(step, ensure_ascii=False), flush=True)
                    if Announcement.query.count():
                        report['state'] = 'partial'
                        if args.stop_after_first_column:
                            break
                from backend.services.inbox import source_hierarchy
                from backend.services.directory_options import directory_entries_for
                def outline(unit):
                    return {'name': unit['name'], 'url': unit.get('url', ''),
                        'columns': [{'name': c['label'], 'url': c['department'].list_url} for c in unit['own_columns']],
                        'children': [outline(child) for child in unit['children']]}
                tree = source_hierarchy(school.departments.all(), directory_entries=directory_entries_for(school.id))
                report.update(ai_calls=provider.call_count,
                    departments=Department.query.filter_by(kind='unit').count(),
                    structure={group: [outline(unit) for unit in units] for group, units in tree.items()},
                    saved_notices=Announcement.query.count(), proposals=SourceProposal.query.count(),
                    columns=[{'name': d.name, 'url': d.list_url} for d in
                             Department.query.filter(Department.list_selector.is_not(None), Department.list_selector != '')],
                    pages=(inventory.report(key) or {}).get('states', {}),
                    elapsed_seconds=round(time.monotonic() - started, 1))
            finally:
                db.session.remove(); db.engine.dispose()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'structure'}, ensure_ascii=False), flush=True)
    return 0 if report['saved_notices'] and report['departments'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--school', required=True)
    parser.add_argument('--url', required=True)
    parser.add_argument('--max-pages', type=int, default=15)
    parser.add_argument('--max-seconds', type=int, default=120)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--stop-after-first-column', action='store_true', help='Parser smoke check only; skips structure completion')
    raise SystemExit(run(parser.parse_args()))
