"""Actual retained HTML/PDFs through acquisition and fenced native onboarding."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import gzip
import hashlib
import json
import time
import unittest

from flask import Flask
from backend.scraper.acquisition import FetchRequest, FetchResult, FetchedHTML, fetch, execution_context
from backend.scraper.acquisition.attachments import pdf_observation

FIXTURES = Path(__file__).parent / 'fixtures/structure/swpu-pdf-notices-20261102'


def corpus():
    data = json.loads((FIXTURES / 'manifest.json').read_text(encoding='utf-8'))
    def original(ref):
        raw = gzip.decompress((FIXTURES / ref['file']).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == ref['sha256']
        return raw
    documents = {data['list_url']: original(data['list']).decode(),
                 data['next_list']['url']: original(data['next_list']).decode()}
    for item in data['articles']:
        documents[item['html']['url']] = original(item['html']).decode()
        documents[item['pdf_url']] = original(item['pdf'])
    return data, documents


class EmbeddedPDFAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.data, self.documents = corpus()
        self.temp = TemporaryDirectory(prefix='embedded-pdf-')
        self.addCleanup(self.temp.cleanup)
        self.app = Flask(__name__)
        self.app.config['SOURCE_GOVERNANCE_EVIDENCE_PATH'] = str(Path(self.temp.name) / 'evidence')
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.calls = []

    def transport(self, request):
        self.calls.append(request.url)
        raw = self.documents[request.url]
        if isinstance(raw, bytes):
            return FetchResult(request.url, status=200, attachment=pdf_observation(raw, request.url, {}, retain=True))
        return FetchResult(request.url, status=200, html=raw)

    def request(self, number=0, **changes):
        row = self.data['articles'][number]
        return FetchRequest(row['html']['url'], purpose='article', browser_allowed=False,
            policy=dict(exploration=True, expected_article_title=row['title'], **changes))

    def test_three_actual_articles_keep_html_and_resolve_their_observed_pdf(self):
        for number, item in enumerate(self.data['articles']):
            result = fetch(self.request(number), http_transport=self.transport)
            self.assertTrue(result.ok, result.error_code)
            self.assertEqual(result.final_url, item['html']['url'])
            self.assertEqual(result.html, self.documents[item['html']['url']])
            self.assertEqual(result.attachment['resource_url'], item['pdf_url'])
            self.assertEqual(result.attachment['sha256'], item['pdf']['sha256'])
            self.assertEqual(result.attachment['embedding']['content_hash'], item['html']['sha256'])
        self.assertEqual(len(self.calls), 6)

    def test_complete_parent_and_resource_observations_are_reused_after_handoff(self):
        cache = {}
        with execution_context(cache_lookup=lambda r: cache.get(r.url), cache_store=lambda r, v: cache.__setitem__(r.url, v)):
            first = fetch(self.request(), http_transport=self.transport)
            self.assertTrue(first.ok)
            again = fetch(self.request(), http_transport=self.transport)
        self.assertTrue(again.ok)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(again.attachment, first.attachment)

    def test_snapshot_restores_both_the_original_page_and_its_pdf_binding(self):
        from backend.services import source_governance as g
        result = fetch(self.request(), http_transport=self.transport)
        ref = g._snapshot(FetchedHTML(result), result.final_url, role='article')
        self.assertEqual(ref['hash'], self.data['articles'][0]['html']['sha256'])
        restored = g.read_snapshot(ref)
        self.assertEqual(restored, result.html)
        self.assertEqual(restored.result.attachment, result.attachment)
        self.assertEqual(restored.final_url, result.final_url)

    def test_snapshot_rejects_missing_changed_pdf_and_unbound_resource(self):
        from backend.services import source_governance as g
        result = fetch(self.request(), http_transport=self.transport)
        ref = g._snapshot(FetchedHTML(result), result.final_url, role='article')
        path = g._evidence_root() / (result.attachment['sha256'] + '.pdf')
        original = path.read_bytes()
        for operation in ('missing', 'changed'):
            with self.subTest(operation=operation):
                if operation == 'missing':
                    path.unlink()
                else:
                    path.write_bytes(original + b'changed')
                with self.assertRaises(ValueError):
                    g.read_snapshot(ref)
                path.write_bytes(original)
        changed = deepcopy(ref)
        changed['embedded_pdf']['embedding']['content_hash'] = '0' * 64
        with self.assertRaises(ValueError):
            g.read_snapshot(changed)

    def test_public_evidence_replay_validates_three_retained_page_pdf_pairs(self):
        from backend.services import source_governance as g
        records, articles, sampled = [], [], {}
        for number, item in enumerate(self.data['articles']):
            result = fetch(self.request(number), http_transport=self.transport)
            ref = g._snapshot(FetchedHTML(result), result.final_url, role='article')
            articles.append(dict(ref, listed_url=item['html']['url']))
            records.append(dict(url=item['html']['url'], title=item['title']))
            sampled['article:' + item['html']['url']] = 'obtained'
        with patch('socket.socket.connect', side_effect=AssertionError('No live website request')):
            checked = g._captured_article_samples(self.data['config'], records,
                                                  dict(articles=articles, samples=sampled))
        self.assertTrue(all(row['matched'] for row in checked['samples']), checked)
        self.assertEqual(len(checked['samples']), 3)
        self.assertEqual({u for row in checked['samples'] for u in row['attachment_urls']},
                         {item['pdf_url'] for item in self.data['articles']})
        self.assertEqual({row['snapshot']['hash'] for row in checked['samples']},
                         {item['html']['sha256'] for item in self.data['articles']})

    def test_acquisition_capture_retains_the_real_parent_and_resource_actions(self):
        from backend.ai.material_capture import capture_materials, _public_url
        with capture_materials(Path(self.temp.name) / 'capture', label='retained-embedded-pdf-check') as captured:
            result = fetch(self.request(), http_transport=self.transport)
        events = [json.loads(p.read_text()) for p in captured.path.glob('event-*.json')]
        finals = [e for e in events if e['event'] == 'acquisition_result']
        self.assertTrue(result.ok)
        self.assertEqual({e['payload']['final_url'] for e in finals},
                         {_public_url(self.data['articles'][0]['html']['url']), _public_url(self.data['articles'][0]['pdf_url'])})
        self.assertEqual(len({e['action_reference']['action_id'] for e in finals}), 2)
        self.assertTrue(captured.complete)

    def test_comment_expression_external_and_ambiguous_loaders_are_not_followed(self):
        item = self.data['articles'][0]
        original = self.documents[item['html']['url']]
        from urllib.parse import urlsplit
        p = urlsplit(item['pdf_url'])
        literal = p.path + '?' + p.query
        variants = [original.replace('showVsbpdfIframe(', '//showVsbpdfIframe('),
                    original.replace('"' + literal + '"', '"' + literal + '" + suffix'),
                    original.replace(literal, 'https://unrelated.edu.cn/body.pdf'),
                    original.replace('</body>', '<div class="v_news_content">' +
                        original.split('<div class="v_news_content">', 1)[1].split('</div>', 1)[0] + '</div></body>')]
        for html in variants:
            with self.subTest(html_hash=hashlib.sha256(html.encode()).hexdigest()):
                calls = []
                def only_parent(request):
                    calls.append(request.url)
                    self.assertEqual(request.url, item['html']['url'])
                    return FetchResult(request.url, status=200, html=html)
                result = fetch(self.request(), http_transport=only_parent)
                self.assertFalse(result.ok)
                self.assertEqual(len(calls), 1)

    def test_child_access_failure_preserves_parent_observation_and_failure_type(self):
        from backend.services.source_validation import sample_failure_kind
        def denied(request):
            if request.url == self.request().url:
                return self.transport(request)
            return FetchResult(request.url, status=403, html='<h1>Forbidden</h1>')
        result = fetch(self.request(), http_transport=denied)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.final_url, self.request().url)
        self.assertEqual(result.error_code, 'http_403')
        self.assertEqual(sample_failure_kind([dict(reason=result.outcome, error_code=result.error_code)]), 'access_restricted')

    def test_html_at_a_pdf_resource_does_not_become_a_valid_attachment(self):
        def wrong(request):
            if request.url == self.request().url:
                return self.transport(request)
            return FetchResult(request.url, status=200, html='<article>这只是另一个网页，并不是所需文件。</article>')
        self.assertFalse(fetch(self.request(), http_transport=wrong).ok)

    def test_parent_deadline_is_shared_before_reading_the_resource(self):
        clock = [0.0]
        def slow_parent(request):
            result = self.transport(request)
            clock[0] = 2.0
            return result
        with patch('backend.scraper.acquisition.coordinator.time.monotonic', side_effect=lambda: clock[0]):
            result = fetch(replace(self.request(), timeout_seconds=1), http_transport=slow_parent)
        self.assertEqual(self.calls, [self.request().url])
        self.assertEqual(result.error_code, 'http_budget_exhausted')

    def test_changed_parent_hash_or_missing_html_cannot_supply_embedding_identity(self):
        from backend.scraper.acquisition.attachments import pdf_sample_identity
        item = self.data['articles'][0]
        result = fetch(self.request(), http_transport=self.transport)
        attachment = deepcopy(result.attachment)
        attachment['embedding']['content_hash'] = '0' * 64
        self.assertFalse(pdf_sample_identity(attachment, item['title'], result.final_url,
            result.final_url, page_html=result.html)['matched'])
        self.assertFalse(pdf_sample_identity(result.attachment, item['title'],
            result.final_url, result.final_url)['matched'])

    def test_opaque_embedded_filename_cannot_replace_the_required_title_witness(self):
        from backend.scraper.acquisition.attachments import pdf_sample_identity
        result = fetch(self.request(), http_transport=self.transport)
        attachment = dict(result.attachment, filename='0610.pdf')
        mismatch = pdf_sample_identity(attachment, '与实际原文完全不同的通知', result.final_url,
            result.final_url, page_html=result.html)
        self.assertFalse(mismatch['matched'])
        self.assertFalse(mismatch['limited_identity'])


class EmbeddedPDFNativeFlowTests(unittest.TestCase):
    def test_actual_trial_rules_validate_three_pdfs_and_install_only_in_isolated_worker(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.database.db import db
        from backend.database.models import BackgroundTask, Department, School
        from backend.database.source_governance_models import SourceValidation
        from backend.services import source_governance as governance, tasks
        from backend.services.source_validation import VALIDATION_VERSION
        from backend.services.discovery_policy import progress_state
        from backend.worker import execute
        data, documents = corpus()
        fixture = DirectOnboardingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        school = db.session.get(School, fixture.school_id)
        school.name, school.url = data['school_name'], data['school_url']
        reference = governance._snapshot(documents[data['list_url']], data['list_url'])
        column = {k: v for k, v in data['config'].items() if k not in ('list_url', 'group_name')}
        job = tasks.enqueue('onboard', 'retained-actual-pdf-column',
            dict(school_id=school.id, url=data['list_url'], ai_assist=True, group_name='', snapshot=reference))
        job.checkpoint = dict(onboarding_page=reference, column_validation=dict(
            page_hash=hashlib.sha256((data['list_url'] + r'\0' + documents[data['list_url']]).encode()).hexdigest(),
            stage='trial', validation_version=VALIDATION_VERSION, trial_columns=[column],
            trial_origin_stage='rules', trial_decision_ids=[], trial_request_key='',
            trial_recognition_issues=[], department_ids=[], completed=[], new_count=0,
            failures=[], progress=progress_state(), progress_ids=[], empty_columns=[], empty_rules=[]))
        db.session.commit()
        requests = []
        def retained(request):
            requests.append(request.url)
            raw = documents[request.url]
            if isinstance(raw, bytes):
                return FetchResult(request.url, status=200, attachment=pdf_observation(raw, request.url, {}, retain=True))
            return FetchResult(request.url, status=200, html=raw)
        handle = tasks.claim(capabilities=['http'])
        self.assertEqual(handle['id'], job.id)
        with patch('backend.scraper.acquisition.coordinator.http_fetch', side_effect=retained), \
                patch('backend.ai.providers.complete', side_effect=AssertionError('No new model call')), \
                patch('socket.socket.connect', side_effect=AssertionError('No live website request')):
            execute(fixture.app, handle)
        job = db.session.get(BackgroundTask, job.id, populate_existing=True)
        self.assertEqual(job.state, 'done', job.result)
        self.assertEqual(job.result['state'], 'connected', job.result)
        validation = json.loads(SourceValidation.query.filter(SourceValidation.department_id.is_not(None)).one().validation_json)
        self.assertEqual(validation['status'], 'full')
        self.assertEqual(validation['pagination']['state'], 'validated')
        self.assertEqual(len(validation['articles']['samples']), 3)
        self.assertTrue(all(s['matched'] for s in validation['articles']['samples']))
        self.assertTrue(all(s.get('snapshot', {}).get('embedded_pdf') for s in validation['articles']['samples']))
        self.assertEqual({u for s in validation['articles']['samples'] for u in s['attachment_urls']},
                         {r['pdf_url'] for r in data['articles']})
        self.assertEqual(Department.query.filter_by(kind='column').count(), 1)
        self.assertEqual(Department.query.filter_by(kind='column').one().group_name, '')
        self.assertTrue(all(r['pdf_url'] in requests for r in data['articles']))


if __name__ == '__main__':
    unittest.main()
