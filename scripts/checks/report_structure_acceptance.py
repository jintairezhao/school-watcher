"""Read desktop acceptance evidence without running workers, websites or models.

Installed rules, validated samples and independently reviewed memberships are
different measurements. Missing transport instrumentation stays unknown.
"""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime


def unpack(value):
    return json.loads(value) if isinstance(value, str) and value else value or {}


def readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def fingerprint(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def transport_measurements(folder):
    paths = sorted(folder.glob('transport-*.jsonl'))
    if not paths:
        return None
    dispatches, counts, versions = {}, Counter(), set()
    for path in paths:
        for line in path.read_text(encoding='utf-8').splitlines():
            event = unpack(line)
            counts[event['event']] += 1
            if event['event'] == 'instrumentation_ready':
                versions.add(event.get('version', 1))
            # A snapshot updates existing attempts; never sum those snapshots.
            for row in event.get('records', []):
                dispatches[row['measurement_id']] = row
    return {'http_transport_attempts': len(dispatches),
            'http_definitely_unsent_proxy_failures': sum(row.get('request_evidence') ==
                'proxy_connection_failed_before_send' for row in dispatches.values()),
            'http_response_observed': sum(row.get('request_evidence') == 'response_received'
                                          for row in dispatches.values()),
            'logical_http_attempts': counts['logical_http_start'],
            'browser_navigation_attempts': counts['browser_navigation_start'],
            'browser_navigation_results': counts['browser_navigation_result'],
            'nonempty_task_fetch_lookups': counts['task_evidence_hit'] + counts['browser_result_lookup'],
            'task_evidence_cache_hits': counts['task_evidence_hit'] if versions == {2} else None,
            'discovery_snapshot_cache_hits': counts['discovery_snapshot_hit'],
            'model_paid_decision_cache_hits': counts['model_paid_decision_cache_hit'],
            'instrumentation_versions': sorted(versions),
            'scope': 'Official HTTP transport and browser navigations; browser subresources are not counted. '
                     'Version 1 nonempty task lookups can include browser polling and are not file-cache-hit counts.'}


def report(folder, oracle):
    folder = folder.resolve()
    completion = unpack((folder / 'completion.json').read_text(encoding='utf-8'))
    if completion.get('status') != 'observed':
        raise ValueError('Only settled observation runs can be compared; inspect the retained failure first')
    manifest = unpack((folder / 'manifest.json').read_text(encoding='utf-8'))
    imported_ids = set((manifest.get('acceptance') or {}).get('imported_execution_ids', []))
    data = folder / 'blank-data'
    main, inventory = data / 'school_watcher.db', data / 'discovery_cache.sqlite3'
    before = {str(path): fingerprint(path) for path in (main, inventory)}
    with closing(readonly(main)) as connection:
        school = dict(connection.execute('SELECT id,name,url FROM schools').fetchone())
        sources = [dict(row) for row in connection.execute(
            "SELECT id,name,group_name,list_url,list_selector FROM departments "
            "WHERE kind='column' AND list_selector IS NOT NULL AND list_selector<>'' ORDER BY id")]
        executions = [dict(row) for row in connection.execute(
            'SELECT execution_id,status,mode,request_sent,usage,reserved_tokens,created_at,finished_at,error_code '
            'FROM ai_executions ORDER BY created_at,execution_id')]
        validations = [dict(row) for row in connection.execute(
            'SELECT id,department_id,config_hash,validation_json,created_at FROM source_validations ORDER BY id')]
        tasks = [dict(row) for row in connection.execute(
            'SELECT id,kind,state,phase,error_code,payload,result FROM background_tasks ORDER BY id')]
        appearances = {}
        for row in connection.execute('SELECT department_id,article_url FROM announcement_sources'):
            appearances.setdefault(row['department_id'], set()).add(row['article_url'])
        notice_count = connection.execute('SELECT count(*) FROM announcements').fetchone()[0]
    with closing(readonly(inventory)) as connection:
        pages = [dict(row) for row in connection.execute(
            'SELECT url,label,kind,state,attempts,content_hash,status_code,health,error,network_failures,checked_at,retry_at '
            'FROM pages ORDER BY checked_at,url')]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        choices = [dict(row) for row in connection.execute('SELECT * FROM frontier_choices')] if 'frontier_choices' in tables else None
    for row in executions:
        row['usage'] = unpack(row['usage'])
    for row in validations:
        row['validation_json'] = unpack(row['validation_json'])
    for row in tasks:
        row['payload'], row['result'] = unpack(row['payload']), unpack(row['result'])
    imported = [row for row in executions if row['execution_id'] in imported_ids]
    sent = [row for row in executions if row['request_sent'] and row['execution_id'] not in imported_ids]
    known = [row for row in sent if row['usage'].get('known')]
    unknown = [row for row in sent if not row['usage'].get('known')]
    if sum(row['usage'].get('total_tokens') or 0 for row in known) != completion['final_known_tokens']:
        raise ValueError('Provider ledger differs from the completed run; reconcile before reporting')
    snapshots = []
    for path in sorted((data / 'fetch-evidence').glob('*.json')):
        value = unpack(path.read_text(encoding='utf-8'))
        snapshots.append({key: value.get(key) for key in
                          ('final_url', 'transport', 'outcome', 'error_code', 'status', 'timings')})
    by_url = {page['url']: page for page in pages}
    references = []
    for reference in oracle.get('sources', []):
        if reference['channel_path'][0] != school['name']:
            continue
        articles = {address for article in reference['reviewed_articles']
                    for address in (article['url'], article.get('final_url')) if address}
        matches = []
        for source in sources:
            overlap = sorted(articles & appearances.get(source['id'], set()))
            if overlap:
                matches.append({'source_id': source['id'], 'installed_name': source['name'],
                                'installed_group_name': source['group_name'], 'list_url': source['list_url'],
                                'reviewed_membership_urls': overlap,
                                'two_memberships_observed': len(overlap) >= 2})
        page = by_url.get(reference['url']) or by_url.get(reference.get('final_url'))
        relevant_tasks = [row for row in tasks if row['payload'].get('url') in
                          (reference['url'], reference.get('final_url')) and row['payload'].get('url')]
        reason = ('reviewed_memberships_observed; attribution_requires_separate_check' if matches else
                  'read_or_access_failure' if page and page['error'] else
                  'pending_onboarding_or_validation' if relevant_tasks else
                  'observed_page_without_matching_memberships' if page else
                  'not_observed_in_this_bounded_window')
        references.append({'path': reference['channel_path'], 'url': reference['url'], 'matches': matches,
                           'reason': reason, 'page': page,
                           'tasks': [{key: row[key] for key in ('id', 'kind', 'state', 'phase', 'error_code', 'result')}
                                     for row in relevant_tasks]})
    stable = all(fingerprint(path) == before[str(path)] for path in (main, inventory))
    trace = transport_measurements(folder)
    start = datetime.fromisoformat(manifest['started_at']).replace(tzinfo=None)
    passed = [row for row in validations if row['department_id'] in {source['id'] for source in sources}
              and row['validation_json'].get('articles', {}).get('passed') is True
              and not row['validation_json'].get('errors')]
    first_source = min(((datetime.fromisoformat(row['created_at']) - start).total_seconds()
                        for row in passed), default=None)
    return {'run': str(folder), 'school': school, 'trace_label': manifest['new_run']['trace_label'],
            'completion': completion, 'same_arm_diagnostic_limits': manifest.get('acceptance', {}).get('diagnostic_limits') or {
                key: manifest.get('acceptance', {}).get(key) for key in
                ('process_local_request_cap', 'process_local_token_cap', 'window_seconds', 'comparison', 'cold_independent')},
            'historical_imported_paid_rows': len(imported),
            'historical_imported_unknown_paid_rows': sum(row['request_sent'] and not row['usage'].get('known') for row in imported),
            'actual_model_requests': len(sent),
            'actual_provider_tokens': sum(row['usage'].get('total_tokens') or 0 for row in known),
            'unknown_paid_execution_ids': [row['execution_id'] for row in unknown],
            'unsettled_reserved_tokens': sum(row['reserved_tokens'] or 0 for row in executions
                                            if row['execution_id'] not in imported_ids and not row['usage'].get('known')),
            'model_modes': dict(Counter(row['mode'] for row in sent)),
            'installed_source_rows': len(sources), 'notice_rows': notice_count,
            'first_passed_source_validation_seconds': first_source,
            'sources': sources, 'executed_validation_records': validations,
            'known_reference_sources': references,
            'known_reference_source_count': len(references),
            'known_sources_with_two_reviewed_memberships': (sum(any(match['two_memberships_observed'] for match in row['matches'])
                for row in references) if references else None),
            'independent_ownership_audit': 'pending; membership overlap and installed names alone do not prove attribution',
            'page_state_counts': dict(Counter(page['state'] for page in pages)),
            'checked_page_count': sum(bool(page['checked_at']) for page in pages),
            'page_claims_including_deferred_attempts': sum(page['attempts'] or 0 for page in pages),
            'pages': pages, 'latest_choice_per_page': choices,
            'retained_fetch_observations': snapshots,
            'actual_http_dispatch_count': None, 'task_evidence_cache_hits': None,
            'discovery_snapshot_cache_hits': None,
            'transport_measurements': trace,
            'measurement_limits': ['Page claims and retained observations are not actual HTTP request counts.',
                                   ('Transport instrumentation is absent; request/cache counts are unknown.' if trace is None else
                                    'Transport counters cover official HTTP dispatch and browser document navigation, not browser subresources.'),
                                   'Known-reference samples retain their original review dates; revalidation is a separate measurement.',
                                   'A bounded observation is not completion of school discovery.'],
            'input_hashes': before, 'input_files_unchanged': stable}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--oracle', type=Path, default=Path(__file__).resolve().parents[2] / 'tests/acceptance/source_agent_oracle.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output == args.oracle.resolve() or any(output.is_relative_to(folder.resolve()) for folder in args.runs):
        raise SystemExit('The report must be outside input runs and must not overwrite reference evidence')
    oracle = unpack(args.oracle.read_text(encoding='utf-8'))
    result = [report(folder, oracle) for folder in args.runs]
    if not all(row['input_files_unchanged'] for row in result):
        raise SystemExit('An input changed during the read; retain evidence and rerun after stopping its owner')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps([{'run': row['trace_label'], 'requests': row['actual_model_requests'],
                       'tokens': row['actual_provider_tokens'], 'installed_source_rows': row['installed_source_rows'],
                       'notices': row['notice_rows'], 'ownership_audit': row['independent_ownership_audit']}
                      for row in result], ensure_ascii=False))
