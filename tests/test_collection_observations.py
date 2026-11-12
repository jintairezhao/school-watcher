"""Collecting evidence is tied to the run/config, not inferred from log names."""
from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests import test_direct_onboarding as fixture_module
from backend.database.db import db
from backend.database.models import Department, ScrapeLog
from backend.scraper.acquisition import FetchFailure, FetchRequest, FetchResult, execution_context, fetch
from backend.services import task_fetch, tasks
from backend.services.source_governance import _hash, source_config

LIST_HTML = '<h2>通知公告</h2><ul>' + ''.join(
    f'<li><a href="/info/{i}.htm">关于课程安排的通知{i}</a><time>2026-11-08</time></li>' for i in range(3)) + '</ul>'


class ObservationTimingTests(unittest.TestCase):
    def test_fresh_failed_read_gets_time_and_is_observed_without_being_cached(self):
        observe, store = Mock(), Mock()
        request = FetchRequest('https://example.edu.cn/list', purpose='list', browser_allowed=False)
        with execution_context(observation_store=observe, cache_store=store):
            result = fetch(request, http_transport=lambda r: FetchResult(r.url,
                outcome='network_error', error_code='http_connect_timeout'))
        self.assertIsNotNone(datetime.fromisoformat(result.observed_at).tzinfo)
        observe.assert_called_once_with(request, result)
        store.assert_not_called()
        self.assertFalse(result.replayable)

    def test_replay_keeps_original_time_and_legacy_unknown_is_not_filled(self):
        for stamp in ('2026-10-01T01:02:03+00:00', ''):
            with self.subTest(stamp=stamp):
                observe = Mock()
                old = FetchResult('https://example.edu.cn/list', status=200, html=LIST_HTML,
                    outcome='usable', observed_at=stamp)
                with execution_context(cache_lookup=lambda r: old, observation_store=observe):
                    result = fetch(FetchRequest(old.final_url, purpose='list'),
                        http_transport=Mock(side_effect=AssertionError('Replay must not read website')))
                self.assertEqual(result.observed_at, stamp)
                observe.assert_not_called()


class CollectionObservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()
        self.root = Path(self.fixture.app.config['SOURCE_CATALOG_PATH']).parent / 'fetch-evidence'
        self.fixture.app.config['FETCH_EVIDENCE_DIR'] = str(self.root)
        self.department = Department(school_id=self.fixture.school_id, name='通知公告', kind='column',
            list_url='https://example.edu.cn/list', list_selector='ul li', title_selector='a',
            link_selector='a', date_selector='time', content_selector='article')
        db.session.add(self.department)
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def handle(self):
        tasks.enqueue('collect', self.department.id, dict(school_id=self.fixture.school_id,
            department_id=self.department.id))
        return tasks.claim()

    def log(self, handle):
        log = ScrapeLog(school_id=self.fixture.school_id, source_name=self.department.name,
            task_id=handle['id'], task_generation=handle['generation'], task_token=handle['token'])
        db.session.add(log)
        db.session.commit()
        return log

    def transport_context(self):
        return execution_context(cache_lookup=task_fetch.cache_lookup, cache_store=task_fetch.cache_store,
            observation_store=task_fetch.record_collection_observation)

    def test_real_worker_dispatch_preserves_run_identity_and_failure_resource(self):
        from backend.worker import dispatch
        handle = self.handle()
        original = source_config(self.department)
        source_id = str(self.department.id)

        def collect(department):
            result = fetch(FetchRequest(department.list_url, purpose='list', source_id=source_id,
                browser_allowed=False), http_transport=lambda r: FetchResult(r.url, status=200, html=LIST_HTML))
            self.assertTrue(result.ok)
            blocked = fetch(FetchRequest('https://example.edu.cn/info/1.htm', purpose='article',
                source_id=source_id, browser_allowed=False), http_transport=lambda r: FetchResult(r.url,
                    status=404, html='not found', headers={'Authorization':'never-record-this'},
                    outcome='denied', error_code='http_404', message='never-record-this'))
            raise FetchFailure(blocked)

        with tasks.execution_scope(handle), self.transport_context(), \
             patch('backend.services.inbox_refresh.subscribed_sources', return_value=[self.department]), \
             patch('backend.services.source_collection.collect_source', side_effect=collect):
            with self.assertRaises(FetchFailure):
                dispatch('collect', dict(handle['payload']))
        log = ScrapeLog.query.one()
        record = task_fetch.read_collection_observations(log)
        self.assertEqual(log.status, 'failed')
        context = record['collection']
        self.assertEqual((context['source_id'], context['school_id'], context['task_id'], context['task_generation']),
            (self.department.id, self.fixture.school_id, handle['id'], handle['generation']))
        self.assertEqual(context['config'], original)
        self.assertEqual(context['config_hash'], _hash(original))
        self.assertEqual([(r['purpose'],r['status'],r['is_configured_entry']) for r in record['observations']],
            [('list',200,True),('article',404,False)])
        payload = task_fetch.collection_observation_path(log).read_text(encoding='utf-8')
        self.assertNotIn('never-record-this', payload)
        self.assertNotIn(LIST_HTML, payload)

    def test_config_bound_cache_replays_once_and_does_not_borrow_another_config(self):
        handle = self.handle()
        log = self.log(handle)
        request = FetchRequest(self.department.list_url, purpose='list', source_id=str(self.department.id), browser_allowed=False)
        with tasks.execution_scope(handle), self.transport_context():
            with task_fetch.collection_observation_context(self.department, log):
                first = fetch(request, http_transport=lambda r: FetchResult(r.url, status=200, html=LIST_HTML))
                again = fetch(request, http_transport=Mock(side_effect=AssertionError('duplicate read')))
                with patch.object(task_fetch,'record_collection_observation',side_effect=OSError('metadata write failed')):
                    with self.assertRaises(OSError):
                        fetch(request,http_transport=Mock(side_effect=AssertionError('metadata failure must not reread')))
            self.assertEqual(first.observed_at, again.observed_at)
            self.assertEqual(len(task_fetch.read_collection_observations(log)['observations']), 1)
            self.department.title_selector = 'a.title'
            changed_log = self.log(handle)
            with task_fetch.collection_observation_context(self.department, changed_log):
                self.assertIsNone(task_fetch.cache_lookup(request))
        self.assertEqual(task_fetch.read_collection_observations(log)['collection']['config']['title_selector'], 'a')

    def test_transient_failure_is_retained_as_metadata_but_does_not_block_retry(self):
        handle = self.handle()
        log = self.log(handle)
        request = FetchRequest(self.department.list_url, purpose='list', browser_allowed=False)
        transport = Mock(side_effect=lambda r: FetchResult(r.url, outcome='network_error', error_code='http_connect_timeout'))
        with tasks.execution_scope(handle), self.transport_context(), task_fetch.collection_observation_context(self.department, log):
            fetch(request, http_transport=transport)
            fetch(request, http_transport=transport)
        self.assertEqual(transport.call_count, 2)
        record = task_fetch.read_collection_observations(log)
        self.assertEqual(len(record['observations']), 2)
        self.assertTrue(all(r['status']==0 and r['error_code']=='http_connect_timeout' for r in record['observations']))
        self.assertFalse(handle.get('checkpoint',{}).get('completed_fetches'))

    def test_resumed_log_links_original_material_without_retimestamping_it(self):
        handle = self.handle()
        first_log = self.log(handle)
        request = FetchRequest(self.department.list_url,purpose='list',browser_allowed=False)
        with tasks.execution_scope(handle), self.transport_context():
            with task_fetch.collection_observation_context(self.department,first_log):
                first = fetch(request,http_transport=lambda r: FetchResult(r.url,status=200,html=LIST_HTML))
            resumed_log = self.log(handle)
            with task_fetch.collection_observation_context(self.department,resumed_log):
                again = fetch(request,http_transport=Mock(side_effect=AssertionError('not a new read')))
        record = task_fetch.read_collection_observations(resumed_log)
        self.assertEqual(again.observed_at,first.observed_at)
        self.assertEqual(record['observations'],[])
        self.assertEqual(len(record['reused_materials']),1)
        reused = record['reused_materials'][0]
        self.assertEqual(reused['observed_at'],first.observed_at)
        self.assertTrue((self.root/reused['cache_file']).is_file())

    def test_browser_handoff_completion_is_observed_once_with_original_context(self):
        tasks.enqueue('collect', self.department.id, dict(school_id=self.fixture.school_id,
            department_id=self.department.id), capability='browser')
        handle = tasks.claim()
        log = self.log(handle)
        request = FetchRequest(self.department.list_url, purpose='list', source_id=str(self.department.id))
        identity = dict(school_id=self.fixture.school_id, source_id=self.department.id,
            config_hash=_hash(source_config(self.department)))
        with tasks.execution_scope(handle), self.transport_context(), task_fetch.collection_observation_context(self.department, log):
            tasks.checkpoint({'browser':dict(original_key=task_fetch.request_key(request),
                request=request.to_dict(), collection_context=identity)})
            response = FetchResult(request.url, status=200, html=LIST_HTML, transport='browser', outcome='usable',
                observed_at='2026-11-08T01:00:00+00:00')
            with patch.object(task_fetch.BrowserClient,'submit',return_value=('done',response)) as submit:
                first = fetch(request, http_transport=Mock(side_effect=AssertionError('no duplicate HTTP')))
                again = fetch(request, http_transport=Mock(side_effect=AssertionError('no duplicate HTTP')))
            submit.assert_called_once()
        record = task_fetch.read_collection_observations(log)
        self.assertEqual(len(record['observations']),1)
        self.assertEqual(record['observations'][0]['transport'],'browser')
        self.assertEqual(first.observed_at,again.observed_at)
        self.assertEqual(first.observed_at,response.observed_at)

    def test_existing_html_and_embedded_pdf_keep_separate_resource_observations(self):
        from tests.test_embedded_pdf_acquisition import corpus
        from backend.scraper.acquisition.attachments import pdf_observation
        data, documents = corpus()
        sample = data['articles'][0]
        handle = self.handle()
        log = self.log(handle)
        request = FetchRequest(sample['html']['url'], purpose='article', source_id=str(self.department.id),
            browser_allowed=False, policy={'exploration':True,'expected_article_title':sample['title']})
        def transport(value):
            raw = documents[value.url]
            return FetchResult(value.url,status=200,attachment=pdf_observation(raw,value.url,{},retain=True)) \
                if isinstance(raw,bytes) else FetchResult(value.url,status=200,html=raw)
        with tasks.execution_scope(handle), self.transport_context(), task_fetch.collection_observation_context(self.department,log):
            result = fetch(request,http_transport=transport)
        self.assertTrue(result.ok,result.error_code)
        record = task_fetch.read_collection_observations(log)
        self.assertEqual({(r['request_url'],r['embedded_pdf']) for r in record['observations']},
            {(sample['html']['url'],False),(sample['pdf_url'],True)})
        self.assertTrue(all(r['observed_at'] and r['status']==200 for r in record['observations']))

    def test_foreign_source_or_stale_lease_cannot_create_our_observation(self):
        handle = self.handle()
        log = self.log(handle)
        result = FetchResult(self.department.list_url, observed_at='2026-11-08T01:00:00+00:00')
        with tasks.execution_scope(handle), task_fetch.collection_observation_context(self.department, log):
            task_fetch.record_collection_observation(FetchRequest(result.final_url,source_id='99999'),result)
            self.assertIsNone(task_fetch.read_collection_observations(log))
            with patch.object(tasks,'assert_owned',side_effect=tasks.LeaseLost('lost')):
                with self.assertRaises(tasks.LeaseLost):
                    task_fetch.record_collection_observation(FetchRequest(result.final_url),result)
        self.assertIsNone(task_fetch.read_collection_observations(log))

    def test_capacity_and_cache_cleanup_leave_explicitly_missing_material(self):
        handle = self.handle()
        log = self.log(handle)
        result = FetchResult(self.department.list_url, observed_at='2026-11-08T01:00:00+00:00')
        with tasks.execution_scope(handle), task_fetch.collection_observation_context(self.department, log):
            with patch('backend.services.storage_policy.capacity_bytes',return_value=1):
                task_fetch.record_collection_observation(FetchRequest(result.final_url), result)
            self.assertIsNone(task_fetch.read_collection_observations(log))
            task_fetch.record_collection_observation(FetchRequest(result.final_url), result)
        path = task_fetch.collection_observation_path(log)
        self.assertTrue(path.is_file())
        tasks.finish(handle, result={})
        task_fetch.prune_fetch_evidence(all_cache=True)
        self.assertFalse(path.exists())
        self.assertIsNone(task_fetch.read_collection_observations(log))

    def test_cache_can_be_written_when_final_path_fits_but_old_temporary_path_does_not(self):
        # Windows' legacy260-character limit must not reject a valid final file
        # because a temporary name repeats its64-character request digest.
        padding = 175 - len(str(self.root.resolve())) - 1
        nested = self.root / ('d' * padding)
        self.fixture.app.config['FETCH_EVIDENCE_DIR'] = str(nested)
        handle = self.handle()
        request = FetchRequest(self.department.list_url,purpose='list')
        with tasks.execution_scope(handle):
            task_fetch.cache_store(request,FetchResult(request.url,status=200,html=LIST_HTML,outcome='usable'))
            self.assertTrue(task_fetch.cache_lookup(request).ok)
        paths = list(nested.glob('*.json'))
        self.assertEqual(len(paths),1)
        self.assertLess(len(str(paths[0])),260)
