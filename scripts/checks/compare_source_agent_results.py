"""Compare real run storage with reviewed official sources, without any API calls."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit


def normal(text):
    return re.sub(r'[\s\W_]+', '', text or '').casefold()


def compare(folder, oracle):
    report = json.loads((folder / 'report.json').read_text(encoding='utf8'))
    with sqlite3.connect((folder / 'test.db').as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        sources = [dict(row) for row in connection.execute("SELECT id,name,list_url,group_name FROM departments WHERE kind='column' AND list_selector IS NOT NULL")]
        memberships = {}
        for row in connection.execute('SELECT department_id,article_url FROM announcement_sources UNION SELECT department_id,url FROM announcements'):
            memberships.setdefault(row['department_id'], set()).add(row['article_url'])
        tasks = [dict(row) for row in connection.execute('SELECT kind,state,phase,error_code,payload,result FROM background_tasks')]
        notices = connection.execute('SELECT count(*) FROM announcements').fetchone()[0]
    index = {}
    for page in report['page_reads']:
        for address in (page['url'], page.get('final_url')):
            if address:
                index.setdefault(address, []).append(page)
    school = report['site']['name']
    checked = []
    for reference in (s for s in oracle['sources'] if s['channel_path'][0] == school):
        articles = {address for article in reference['reviewed_articles'] for address in
                    (article['url'], article.get('final_url')) if address}
        candidates = []
        for source in sources:
            overlap = memberships.get(source['id'], set()) & articles
            if len(overlap) >= 2:
                candidates.append({'source_id': source['id'], 'installed_name': source['name'],
                    'installed_list_url': source['list_url'], 'matching_reviewed_article_urls': sorted(overlap),
                    'literal_column_name_matches': normal(source['name']) == normal(reference['column_evidence']['text']),
                    'publisher_name_matches': normal(source['group_name']) == normal(reference['channel_path'][1])})
        addresses = {reference['url'], reference.get('final_url')} - {None}
        reads = [row for address in addresses for row in index.get(address, [])]
        relevant_tasks = [task for task in tasks if json.loads(task['payload']).get('url') in addresses]
        if candidates:
            reason = 'matched_reviewed_article_membership; attribution flags require separate inspection'
        elif any(task['error_code'] == 'budget_exhausted' for task in relevant_tasks):
            reason = 'budget_exhausted'
        elif any(row.get('outcome') in ('network_error', 'denied', 'unavailable') for row in reads):
            reason = 'read_failed_or_access_limited'
        elif relevant_tasks:
            reason = 'not_installed; see_task_result'
        elif reads:
            reason = 'read_but_no_matching_installed_column'
        else:
            reason = 'not_read_within_observation_window; frontier_and_budget_remain_saved'
        checked.append({'channel_path': reference['channel_path'], 'reference_url': reference['url'],
            'matching_sources': candidates, 'reason': reason,
            'read_outcomes': [{'url': row['url'], 'outcome': row.get('outcome'), 'error_code': row.get('error_code')} for row in reads],
            'tasks': [{'kind': task['kind'], 'state': task['state'], 'phase': task['phase'], 'error_code': task['error_code'],
                       'result': json.loads(task['result'])} for task in relevant_tasks]})
    requests = report['requests']
    return {'implementation': report['implementation'], 'site': report['site'],
        'measurement_windows': report.get('measurement_windows', []), 'started_at': report['started_at'],
        'ended_at': report.get('ended_at'), 'elapsed_seconds': report['elapsed_seconds'],
        'actual_model_dispatches': len(requests), 'actual_provider_tokens': sum(r.get('usage', {}).get('total_tokens', 0) for r in requests),
        'unknown_usage_requests': sum(not r.get('usage', {}).get('known') for r in requests),
        'installed_column_rows': len(sources), 'saved_notice_rows': notices,
        'logical_http_reads': len(report['page_reads']), 'http_transport_attempts': len(report.get('http_requests', [])),
        'task_evidence_cache_hits': report['cache_hits'], 'discovery_snapshot_lookups': report.get('discovery_cache_hits', 0),
        'budget_stopped': bool(report.get('budget_stopped')),
        'known_source_count': len(checked), 'sources_with_two_reviewed_article_memberships': sum(bool(row['matching_sources']) for row in checked),
        'task_state_counts': dict(Counter(task['state'] for task in tasks)),
        'task_error_counts': dict(Counter(task['error_code'] for task in tasks if task['error_code'])),
        'known_sources': checked,
        'limitations': ['Membership overlap does not independently prove publisher or column attribution.',
            'No overlap with these sampled articles is an observed miss, not proof a source can never be connected.',
            'Runs on different dates/windows can see different site contents. Cache counters have version-specific definitions; they are not avoided-read percentages.',
            'HTTP dispatch attempts do not prove server receipt. Browser subresource requests are not included.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', type=Path, nargs='+')
    parser.add_argument('--oracle', type=Path, default=Path(__file__).resolve().parents[2] / 'tests/acceptance/source_agent_oracle.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = [compare(folder.resolve(), json.loads(args.oracle.read_text(encoding='utf8'))) for folder in args.runs]
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    print(json.dumps([{'run': row['implementation'], 'known': row['known_source_count'],
                      'matched_memberships': row['sources_with_two_reviewed_article_memberships']} for row in result]))
