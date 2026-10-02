"""Resumable structural discovery. A crawl slice is never a completeness verdict."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from filelock import FileLock

from backend.scraper.http_client import same_school_url
from backend.services.source_inventory import Inventory, canonical_url, has_fragment_route
from .structure import extract_structure, publication_evidence, health_from_evidence, add_publication_structure
from .parser_revision import PARSER_REVISION, assert_current_parser, ParserRevisionChanged

def fetch_page(url, *, purpose='directory', source_id='', readiness_selector=''):
    """Directory and list callers share the same classification/rendering policy."""
    import os
    from backend.scraper.acquisition import FetchRequest, fetch
    result = fetch(FetchRequest(url=url, purpose=purpose, source_id=str(source_id),
                   readiness_selector=readiness_selector,
                   policy={'exploration': True},
                   browser_allowed=os.environ.get('WATCHER_BROWSER', '1') != '0'))
    notes = list(result.evidence)
    network = next(({'public_ip': note.split(':', 1)[1]} for note in notes
                    if isinstance(note, str) and note.startswith('public_dns_ipv4:')), None)
    return {'url': result.final_url, 'status': result.status,
            'html': result.html if result.ok or result.outcome == 'needs_adapter' else '',
            'error': result.message or result.error_code, 'outcome': result.outcome,
            'result': result, 'transport': result.transport, 'network': network, 'network_notes': notes}


def inspect_page(inventory, site, page, fetcher=fetch_page):
    key, url = site['site_key'], page['url']
    assert_current_parser()
    from .structure import document_reference, directory_document
    if page['kind'] != 'root' and document_reference(url, page['label']):
        is_roster = directory_document(page['label'], page['kind'])
        note = 'directory_document_requires_adapter' if is_roster else 'document_reference_only'
        inventory.finish(key, url, state='blocked' if is_roster else 'reference_only',
            error=note if is_roster else None, notes_json=json.dumps([note]))
        return
    try:
        response = fetcher(url)
        assert_current_parser()
        if response.get('outcome') == 'needs_manual' and response.get('result') is not None:
            from backend.scraper.acquisition import FetchFailure
            raise FetchFailure(response['result'])
        html = response['html']
        final = canonical_url(response.get('url') or url)
        if not html:
            inventory.finish(key, url, state='failed', status_code=response.get('status'),
                             final_url=final, error=response.get('error') or 'empty_response',
                             health='dynamic_content' if response.get('outcome') in ('requires_render', 'needs_manual') else 'unreachable')
            return
        content_hash = hashlib.sha256(html.encode()).hexdigest()
        if (has_fragment_route(url) or has_fragment_route(final)) and response.get('transport') != 'browser':
            # HTTP only retrieves the shared application shell, not the route's
            # rendered units or articles. Preserve it for browser adaptation but
            # do not attribute its links, feed or redirect to this route.
            inventory.record_edges(key, url, [], content_hash)
            inventory.record_structure(key, url, [], content_hash)
            notes = ['fragment_route_requires_browser', *response.get('network_notes', [])]
            if response.get('network'):
                notes.append('public_dns_ipv4:' + response['network']['public_ip'])
            inventory.finish(key, url, html=html, fetched_at=response.get('checked_at'),
                             state='fetched', status_code=response.get('status'), final_url=final,
                             title='', error=None, health='dynamic_content', latest_publication=None,
                             feed_json=None, notes_json=json.dumps(notes), parser_revision=PARSER_REVISION)
            return
        parsed = extract_structure(html, final, site['root_url'], page['kind'], page['label'],
                                   json.loads(page['path_json']))
        if parsed.get('branding_candidates'):
            from backend.services.source_ownership import BRANDING_PREFIX
            parsed['notes'].append(BRANDING_PREFIX + json.dumps({
                'site_key': key, 'reference_url': url, 'final_url': final,
                'content_hash': content_hash, 'candidates': parsed['branding_candidates']}, ensure_ascii=False))
        if response.get('network'):
            parsed['notes'].append('public_dns_ipv4:' + response['network']['public_ip'])
        parsed['notes'].extend(response.get('network_notes', []))
        if final != url:
            parsed['notes'].append('redirect:' + final)
        # A third-party redirect or link is evidence to review, not a reason to crawl an unrelated site.
        external = not same_school_url(final, site['root_url'])
        external_confirmed = False
        if external:
            from backend.services.source_ownership import identity_evidence, saved_domain_evidence, PREFIX
            identity = identity_evidence(html, site, page, final, content_hash)
            if identity:
                parsed['notes'].append(PREFIX + json.dumps(identity, ensure_ascii=False))
                external_confirmed = True
            else:
                prior = saved_domain_evidence(inventory, key, final)
                if prior and prior['reference_url'] != page['url']:
                    parsed['notes'].append('external_owner_reference:' + prior['reference_url'])
                    external_confirmed = True
            if not external_confirmed:
                parsed['notes'].append('external_ownership_requires_review')
        feed = publication_evidence(html, final)
        add_publication_structure(parsed, feed)
        if feed:
            for column in feed.get('lists', [feed]):
                if not column.get('name'):
                    parsed['notes'].append('publication_heading_requires_review:' + column.get('container_locator', ''))
                target = column.get('column_url')
                if target and column.get('name'):
                    decision = 'follow' if same_school_url(target, site['root_url']) else 'official_external_link'
                    if external and not same_school_url(target, final):
                        decision = 'external_review'
                    parsed['links'].append({'url': target, 'label': column['name'], 'kind': 'channel',
                                            'path': json.loads(page['path_json']) + [page['label']],
                                            'locator': column['column_link_locator'], 'decision': decision})
        assert_current_parser()
        inventory.record_edges(key, url, parsed['links'], content_hash)
        inventory.record_structure(key, url, parsed['nodes'], content_hash)
        for link in parsed['links']:
            own_external_link = (external_confirmed and link['decision'] == 'external_review'
                                 and same_school_url(link['url'], final))
            if link['decision'] not in ('follow', 'official_external_link') and not own_external_link:
                continue
            if external and not external_confirmed:
                continue
            target = link['url']
            authority = 'school_domain' if same_school_url(target, site['root_url']) else 'official_backlink'
            # An externally hosted official unit may explore its own domain, with reciprocal evidence.
            if external_confirmed and same_school_url(target, final):
                authority = 'reciprocal_official_unit'
            inventory.enqueue(key, target, link['label'], link['kind'], page['depth'] + 1,
                              link['path'] + ([page['label']] if page['kind'] == 'unit' else []), authority)
        latest = feed.get('latest_publication') if feed else None
        health = health_from_evidence(html, latest)
        if health in ('stale', 'infrequent') and feed and not all(
                column.get('publication_dates_complete') for column in feed.get('lists', [feed])):
            health = 'date_unknown'
            parsed['notes'].append('publication_dates_incomplete')
        if page['kind'] == 'channel' and not feed:
            parsed['notes'].append('publication_list_requires_adapter')
        from flask import has_app_context
        snapshot_ref = None
        if has_app_context():
            from backend.services.source_governance import _snapshot
            snapshot_ref = _snapshot(html, final, role='structure')
            parsed['notes'].append('full_snapshot:' + json.dumps(snapshot_ref, ensure_ascii=False))
        inventory.finish(key, url, html=html, fetched_at=response.get('checked_at'),
                         state='fetched', status_code=response.get('status'),
                         final_url=final, title=parsed['title'], error=None,
                         health=health, latest_publication=latest, parser_revision=PARSER_REVISION,
                         feed_json=json.dumps(feed, ensure_ascii=False) if feed else None,
                         notes_json=json.dumps(parsed['notes'], ensure_ascii=False))
        # Known navigation is already executable. AI only handles a missing
        # structural route; column extraction has its own narrow page contract.
        routes = [link for link in parsed['links'] if link['decision'] in ('follow', 'official_external_link')]
        needed = ({'directory', 'unit'} if page['kind'] in ('root', 'directory') else {'channel'})
        if page['kind'] in ('root', 'directory', 'unit') and not any(link['kind'] in needed for link in routes) and not (page['kind'] == 'unit' and feed):
            from .ai_navigation import queue_navigation
            queue_navigation(site, dict(page, url=final, snapshot_url=url, snapshot_ref=snapshot_ref))
    except ParserRevisionChanged:
        raise
    except ValueError as exc:
        inventory.finish(key, url, state='blocked', health='unreachable', error=str(exc)[:800])
    except Exception as exc:
        from backend.scraper.acquisition import FetchFailure
        # Storage, schema and parser exceptions are program failures. Never
        # overwrite a fetched school's page with a fabricated "unreachable".
        if not isinstance(exc, FetchFailure) or exc.outcome == 'needs_manual' and page['kind'] == 'root':
            raise
        if exc.outcome == 'needs_manual':
            inventory.finish(key, url, state='blocked', health='dynamic_content',
                status_code=exc.result.status, final_url=exc.result.final_url,
                error='access_verification_required', notes_json=json.dumps(['access_verification_required']))
            return
        inventory.finish(key, url, state='failed', health='unreachable',
                         error=f'{type(exc).__name__}: {str(exc)[:700]}')


def crawl_site(inventory, key, max_pages=250, workers=4, fetcher=fetch_page, progress=None, retry_failed=False, focus='all'):
    assert_current_parser()
    site = next(s for s in inventory.all_sites() if s['site_key'] == key)
    lock_path = inventory.path.parent / ('source-crawl-' + key + '.lock')
    processed = 0
    with FileLock(str(lock_path), timeout=0):
        inventory.recover(key)
        if retry_failed:
            inventory.retry(key)
        inventory.reconcile_article_references(key)
        try:
            from backend.services.tasks import current_execution
            # ContextVars, ORM sessions and task ownership cannot cross arbitrary
            # executor threads. Durable directory jobs checkpoint one page at a time.
            durable = current_execution() is not None
            if durable:
                workers = 1
            executor = nullcontext(None) if durable else ThreadPoolExecutor(max_workers=workers, thread_name_prefix='source-inventory')
            with executor as pool:
                while max_pages is None or processed < max_pages:
                    if durable:
                        from backend.services.discovery_control import pause_if_requested
                        pause_if_requested()
                    assert_current_parser()
                    allowance = workers if max_pages is None else min(workers, max_pages - processed)
                    batch = [p for _ in range(allowance) if (p := inventory.claim(key, focus=focus)) is not None]
                    if not batch:
                        break
                    if durable:
                        from backend.services.onboarding_progress import record_progress
                        record_progress(phase='crawl', current_label=batch[0]['label'])
                    futures = [pool.submit(inspect_page, inventory, site, p, fetcher) for p in batch] if pool else batch
                    for future in as_completed(futures) if pool else futures:
                        if pool:
                            future.result()
                        else:
                            inspect_page(inventory, site, future, fetcher)
                        processed += 1
                        if progress:
                            progress(processed, inventory.progress_snapshot(key))
        except ParserRevisionChanged:
            # The pool has finished before releasing claims. A local deployment
            # is not a failure of these official websites.
            inventory.recover(key)
            raise
        assert_current_parser()
        inventory.reconcile_article_references(key)
        inventory.settle(key)
    result = inventory.report(key)
    result['processed_this_run'] = processed
    result['discovery_focus'] = focus
    return result
