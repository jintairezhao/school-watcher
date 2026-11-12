"""Keep a declared sampling exclusion local to its exact inventory page.

This is an experiment guard, not a production branch-return rule. Resource
limits and durable browser/source handoffs still propagate unchanged.
"""
from contextlib import contextmanager
import json


@contextmanager
def page_scope_exclusions(bounds):
    if not bounds.scope.get('park_out_of_scope_tasks'):
        yield
        return
    from unittest.mock import patch
    from backend.scraper.discovery import inventory_crawler
    from backend.services import tasks
    original = inventory_crawler._inspect_page

    def inspect(inventory, site, page, *args, **kwargs):
        try:
            return original(inventory, site, page, *args, **kwargs)
        except tasks.TaskDeferred as error:
            handle = tasks.current_execution() or {}
            active = (handle.get('checkpoint') or {}).get('discovery_active_page') or {}
            if (error.error_code != 'sampling_outside_declared_host'
                    or (error.state, error.phase) != ('waiting', 'sampling_scope')
                    or handle.get('kind') not in ('discover', 'directory')
                    or active != {'site_key':site['site_key'], 'url':page['url']}):
                raise
            from scripts.checks.run_native_branch_pilot import read, save
            ledger = read(bounds.web_file)
            exclusions = ledger.get('scope_exclusions') or []
            excluded = exclusions[-1] if exclusions else {}
            if (excluded.get('reason') != 'outside_declared_host'
                    or excluded.get('task_id') != handle.get('id')
                    or excluded.get('generation') != handle.get('generation')):
                raise
            document_id = excluded.get('document')
            document = None
            if document_id is not None:
                document = next((row for row in ledger['documents'] if row['sequence']==document_id), None)
                if not document or document['request']['url'] != page['url']:
                    raise
            elif excluded.get('url') != page['url']:
                raise
            tasks.assert_owned(handle)
            current = inventory.get_page(site['site_key'], page['url']) or {}
            notes = json.loads(current.get('notes_json') or '[]')
            if not isinstance(notes,list):
                raise ValueError('Expected retained inventory notes to be a list')
            evidence = dict(excluded, site_key=site['site_key'], inventory_url=page['url'],
                            page_only=True, semantic_value='unknown')
            note = 'sampling_scope_exclusion:' + json.dumps(evidence,ensure_ascii=False,sort_keys=True)
            if note not in notes:
                notes.append(note)
            inventory.finish(site['site_key'],page['url'],state='scope_excluded',health='unknown',
                error='sampling_outside_declared_host',notes_json=json.dumps(notes,ensure_ascii=False))
            # No HTML/status/result is invented. The unfinished read and the
            # rejected target remain available to later material audits.
            excluded.update(evidence)
            if document is not None:
                document['sampling_exclusion'] = evidence
            if (ledger.get('boundary') or {}).get('reason') == 'outside_declared_host':
                ledger['boundary'] = None
            save(bounds.web_file,ledger)

    with patch.object(inventory_crawler,'_inspect_page',inspect):
        yield
