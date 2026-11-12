"""Bounded reference collection, not a threshold evaluator or source installer.

Only school roots are inputs. Human target labels stay outside this process.
Uses the production structure parser and ordering hints without opening an app,
Inventory, database, browser, or model client. Every link occurrence is retained.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
import argparse
import gzip
import hashlib
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from bs4 import BeautifulSoup

from backend.scraper.acquisition.classifier import classify_result
from backend.scraper.acquisition.contracts import FetchRequest, FetchResult
from backend.scraper.discovery.navigation_order import explain_candidates
from backend.scraper.discovery.structure import (
    extract_structure, publication_evidence, add_publication_structure)
from backend.scraper.http_client import same_school_url, validate_public_url


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(url):
    validate_public_url(url, resolve=False)
    part = urlsplit(url)
    return urlunsplit((part.scheme, part.netloc.lower(), part.path or '/', part.query, ''))


def validate_plan(plan):
    if set(plan) != {'schema', 'schools', 'max_documents', 'max_http_attempts', 'reuse_audit'}:
        raise ValueError('Plan contains unsupported fields; target annotations must stay separate')
    if plan['schema'] != 1 or not isinstance(plan['reuse_audit'], str) or not plan['reuse_audit']:
        raise ValueError('Plan needs a schema and explicit material-reuse audit')
    if not 1 <= plan['max_documents'] <= 24 or not 1 <= plan['max_http_attempts'] <= 30:
        raise ValueError('This collector is limited to 24 documents / 30 HTTP attempts')
    if not isinstance(plan['schools'], list) or not 1 <= len(plan['schools']) <= 4:
        raise ValueError('Choose 1–4 school roots')
    ids, domains = set(), set()
    for school in plan['schools']:
        if set(school) != {'id', 'name', 'root_url', 'max_documents'}:
            raise ValueError('School inputs contain root identity and quota only')
        if not school['id'] or school['id'] in ids or not school['name']:
            raise ValueError('School identities must be unique')
        if not 1 <= school['max_documents'] <= plan['max_documents']:
            raise ValueError('Invalid per-school quota')
        url = canonical(school['root_url'])
        domain = (urlsplit(url).hostname or '').removeprefix('www.')
        if domain in domains:
            raise ValueError('Repeated school root')
        domains.add(domain)
        ids.add(school['id'])
    return plan


def inspect_material(html, page_url, school, entry, digest, observed_at, status=200, headers=None, transport='http'):
    """Pure analysis of currently acquired bytes; never fetches a target."""
    request = FetchRequest(page_url, purpose='list' if entry['kind'] == 'channel' else 'directory')
    result = classify_result(request, FetchResult(final_url=page_url, status=status,
        html=html, headers=headers or {}, transport=transport))
    soup = BeautifulSoup(html, 'lxml')
    analysis = {'outcome': result.outcome, 'error_code': result.error_code,
        'evidence': list(result.evidence), 'title': soup.title.get_text(' ', strip=True) if soup.title else '',
        'semantic_value': 'unknown', 'links': [], 'candidates': [], 'publication_lists': []}
    if not result.ok:
        return analysis
    parsed = extract_structure(html, page_url, school['root_url'], entry['kind'],
        entry['label'], entry.get('path', []))
    publication = publication_evidence(html, page_url)
    add_publication_structure(parsed, publication)
    analysis['publication_lists'] = (publication or {}).get('lists', [])
    page = {'url': page_url, 'kind': entry['kind'], 'label': entry['label'], 'title': parsed['title'],
        'state': 'fetched', 'status_code': status, 'content_hash': digest,
        'observed_structure': parsed['nodes']}
    pairs, rows = [], []
    for link in parsed['links']:
        row = dict(link, source_url=page_url, content_hash=digest, observed_at=observed_at,
                   school_id=school['id'], collection_decision='not_followed', semantic_value='unknown')
        try:
            target = canonical(link['url'])
            same_school = same_school_url(target, school['root_url'])
        except ValueError:
            target, same_school = '', False
        if link['decision'] != 'follow':
            row['collection_decision'] = link['decision']
        elif not same_school:
            row['collection_decision'] = 'outside_this_school_scope'
        else:
            row['collection_decision'] = 'eligible_navigation'
            pairs.append((dict(link, url=target), link['label']))
            rows.append(row)
        analysis['links'].append(row)
    for explanation in explain_candidates(pairs, page):
        index = explanation['original_index']
        link = pairs[index][0]
        analysis['candidates'].append(dict(link, source=rows[index], ordering=explanation))
    analysis['structure_nodes'] = parsed['nodes']
    analysis['parser_notes'] = parsed['notes']
    return analysis


class Collector:
    def __init__(self, plan, output, fetch=None):
        self.plan = validate_plan(plan)
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.fetch = fetch or self.http_fetch
        self.session = None
        self.attempts, self.observations, self.links, self.decisions = [], [], [], []
        self.queues, self.seen, self.counts = {}, {}, Counter()
        for school in plan['schools']:
            self.queues[school['id']] = [{'url': canonical(school['root_url']), 'label': school['name'],
                'kind': 'root', 'path': [], 'source': None, 'ordering': None, 'priority': (-1, 0, 0)}]
            self.seen[school['id']] = set()

    def save(self, name, value):
        (self.output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', 'utf-8')

    def http_fetch(self, url):
        # Explicit redirects, no hidden adapter retries, no proxy or browser fallback.
        validate_public_url(url)
        if self.session is None:
            self.session = requests.Session()
            self.session.trust_env = False
            self.session.headers.update({'User-Agent': 'SchoolWatcher-ReferenceAudit/2026.10',
                'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'zh-CN,zh;q=0.8,en;q=0.5'})
        time.sleep(.5)
        with self.session.get(url, timeout=(5, 10), allow_redirects=False, stream=True) as response:
            chunks, length = [], 0
            for chunk in response.iter_content(65536):
                length += len(chunk)
                if length > 2*1024*1024:
                    raise ValueError('body_limit_exceeded')
                chunks.append(chunk)
            return response.status_code, dict(response.headers), b''.join(chunks)

    def acquire(self, school, entry):
        sid, url = school['id'], entry['url']
        original, started, body, status, headers, failure = url, now(), None, 0, {}, None
        for hop in range(5):
            if len(self.attempts) >= self.plan['max_http_attempts']:
                failure = 'http_budget_exhausted'
                break
            attempt = {'sequence': len(self.attempts)+1, 'school_id': sid, 'url': url, 'started_at': now()}
            self.attempts.append(attempt)
            timer = time.monotonic()
            try:
                status, headers, raw = self.fetch(url)
                attempt['status'] = status
                headers = {str(k).lower(): v for k, v in headers.items()}
                digest = sha(raw)
                (self.output/(digest+'.html.gz')).write_bytes(gzip.compress(raw, mtime=0))
                attempt.update(body_sha256=digest, body_bytes=len(raw), body_file=digest+'.html.gz')
                if status in (301, 302, 303, 307, 308):
                    target = canonical(urljoin(url, headers.get('location', '')))
                    attempt['redirect_target'] = target
                    if not headers.get('location') or not same_school_url(target, school['root_url']):
                        failure = 'redirect_outside_school_scope'
                        break
                    if target in self.seen[sid]:
                        failure = 'redirect_to_already_attempted_page'
                        break
                    self.seen[sid].add(target)
                    url = target
                    continue
                body = raw
                break
            except (requests.RequestException, ValueError) as error:
                failure = type(error).__name__+': '+str(error)[:350]
                attempt['failure'] = failure
                break
            finally:
                attempt.update(finished_at=now(), seconds=round(time.monotonic()-timer, 6))
                self.save('http-attempts.json', self.attempts)
        else:
            failure = 'redirect_limit_exceeded'
        observation = {'sequence': len(self.observations)+1, 'school_id': sid,
            'requested_url': original, 'final_url': url, 'entry': entry,
            'started_at': started, 'observed_at': now(), 'status': status, 'headers': headers,
            'failure': failure, 'semantic_value': 'unknown', 'independent_target_verified': False}
        if body is not None:
            digest = sha(body)
            soup = BeautifulSoup(body, 'lxml')
            html = body.decode(soup.original_encoding or 'utf-8', errors='replace')
            analysis = inspect_material(html, url, school, entry, digest, observation['observed_at'], status, headers)
            observation.update(body_sha256=digest, body_file=digest+'.html.gz', body_bytes=len(body),
                               detected_encoding=soup.original_encoding, analysis=analysis)
            self.links.extend(analysis['links'])
            for candidate in analysis['candidates']:
                target = candidate['url']
                if target not in self.seen[sid]:
                    self.queues[sid].append(dict(candidate,
                        priority=tuple(candidate['ordering']['rank'][:2])+(len(self.links)+len(self.queues[sid]),)))
        else:
            observation['outcome'] = 'unknown_after_acquisition_failure'
        self.observations.append(observation)
        self.counts[sid] += 1
        self.save('observations.json', self.observations)
        self.save('links.json', self.links)

    def run(self):
        self.save('scope.json', dict(plan=self.plan, declared_at=now(), model_calls=0,
            browser_navigations=0, business_writes=0, target_annotations_loaded=False,
            purpose='Reference material collection only, not policy effectiveness evaluation'))
        code = ['scripts/checks/collect_reference_materials.py', 'backend/scraper/acquisition/classifier.py',
                'backend/scraper/discovery/structure.py', 'backend/scraper/discovery/navigation_order.py',
                'backend/scraper/discovery/attention.py', 'backend/scraper/discovery/publication_lists.py']
        frozen = {name: sha((ROOT/name).read_bytes()) for name in code}
        self.save('frozen-code.json', frozen)
        try:
            while len(self.observations) < self.plan['max_documents'] and len(self.attempts) < self.plan['max_http_attempts']:
                progress = False
                for school in self.plan['schools']:
                    sid = school['id']
                    if self.counts[sid] >= school['max_documents']:
                        continue
                    queue = self.queues[sid]
                    queue[:] = [entry for entry in queue if entry['url'] not in self.seen[sid]]
                    if not queue:
                        continue
                    queue.sort(key=lambda entry: entry['priority'])
                    entry = queue.pop(0)
                    self.decisions.append({'step': len(self.observations)+1, 'school_id': sid,
                        'selected': entry, 'known_frontier': [entry, *queue], 'decided_at': now(),
                        'basis': 'School round robin; existing observed-navigation ordering hints'})
                    self.save('collection-decisions.json', self.decisions)
                    self.seen[sid].add(entry['url'])
                    self.acquire(school, entry)
                    progress = True
                    if len(self.observations) >= self.plan['max_documents'] or len(self.attempts) >= self.plan['max_http_attempts']:
                        break
                if not progress:
                    break
        finally:
            if self.session is not None:
                self.session.close()
            self.save('remaining-frontiers.json', self.queues)
        assert all(sha((ROOT/name).read_bytes()) == digest for name, digest in frozen.items())
        summary = {'documents': len(self.observations), 'http_attempts': len(self.attempts),
            'documents_by_school': dict(self.counts), 'recorded_link_occurrences': len(self.links),
            'model_calls': 0, 'browser_navigations': 0, 'business_writes': 0,
            'causal_branch_sample': False, 'independent_holdout_certified': False,
            'unknown_is_not_negative': True, 'threshold_recommendation': 'insufficient_evidence'}
        self.save('summary.json', summary)
        self.save('artifact-hashes.json', {p.name: sha(p.read_bytes()) for p in sorted(self.output.iterdir()) if p.is_file()})
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text('utf-8'))
    print(json.dumps(Collector(plan, args.output).run(), ensure_ascii=False))


if __name__ == '__main__':
    main()
