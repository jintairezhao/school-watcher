"""A branch cannot borrow school-wide progress or reset its clock per page."""
from datetime import datetime
import hashlib
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department
from backend.scraper.discovery.ai_navigation import navigation_progress, process_navigation
from backend.services import tasks
from backend.services.discovery_cache import DiscoveryCache
from tests import test_ai_primary_navigation as navigation_fixture

ROOT = navigation_fixture.ROOT


class NavigationBranchProgressTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.store = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        self.key = self.store.ensure_site('测试大学', ROOT)
        self.page(ROOT, 'root', 0)
        self.page(ROOT + 'library/', 'unit', 1)
        self.page(ROOT + 'office/', 'unit', 1)
        self.edges(ROOT, [ROOT + 'library/', ROOT + 'office/'])

    def tearDown(self):
        self.fixture.tearDown()

    def page(self, url, kind, depth, html='<main>已实际读取的官网页面</main>'):
        self.store.enqueue(self.key, url, url, kind, depth, [], 'school_domain')
        self.store.finish(self.key, url, state='fetched', html=html)
        return self.store.get_page(self.key, url)

    def edges(self, parent, targets, *, decision='follow'):
        self.store.record_edges(self.key, parent, [
            {'url': url, 'label': url, 'kind': self.store.get_page(self.key, url)['kind'],
             'path': [], 'locator': f'main a:nth-of-type({index + 1})', 'decision': decision}
            for index, url in enumerate(targets)], self.store.get_page(self.key, parent)['content_hash'])

    def test_shared_home_menu_and_reference_edges_do_not_import_another_branch(self):
        local = ROOT + 'library/current/'
        foreign = ROOT + 'office/notices/'
        self.page(local, 'channel', 2)
        self.page(foreign, 'channel', 2)
        self.edges(ROOT + 'library/', [local])
        self.edges(ROOT + 'office/', [foreign])
        # A real template's return-to-home route and a sibling resource link.
        self.edges(local, [ROOT, foreign])
        source = Department(school_id=self.fixture.school_id, name='另一部门通知',
                            list_url=foreign, list_selector='li')
        db.session.add(source)
        db.session.flush()
        db.session.add(Announcement(school_id=self.fixture.school_id, department_id=source.id,
                                   title='另一部门已收集的通知', url=ROOT + 'article/foreign'))
        db.session.commit()
        progress = navigation_progress(self.store, self.key, self.fixture.school_id, local)
        foreign_hash = 'page:' + hashlib.sha256((foreign + ':' +
            self.store.get_page(self.key, foreign)['content_hash']).encode()).hexdigest()
        self.assertNotIn(foreign_hash, progress)
        self.assertNotIn('source:' + str(source.id), progress)
        self.assertFalse(any(value.startswith('notice:') for value in progress))
        own_hash = 'page:' + hashlib.sha256((local + ':' +
            self.store.get_page(self.key, local)['content_hash']).encode()).hexdigest()
        self.assertIn(own_hash, progress)

    def navigation_jobs(self, count=3, items=32):
        pages = []
        for index in range(count):
            url = ROOT + f'library/channel-{index}/'
            html = '<nav>' + ''.join(
                f'<a href="/space/{index}/{item}">成长空间{index}-{item}</a>' for item in range(items)) + '</nav>'
            pages.append(self.page(url, 'channel', 2, html))
        self.edges(ROOT + 'library/', [page['url'] for page in pages])
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        parent.state = 'done'
        parent.capability = 'http'
        db.session.commit()
        site = self.store.report(self.key)['site']
        jobs = [tasks.enqueue('navigation_review', f'branch-{index}', {
            'parent_task_id': parent.id, 'parent_generation': parent.generation,
            'school_id': self.fixture.school_id, 'school_generation': parent.generation,
            'site': site, 'page': dict(page, discovery_policy='valuable')})
            for index, page in enumerate(pages)]
        return jobs

    def finish_navigation(self):
        completed = []
        for _ in range(30):
            handle = tasks.claim(capabilities=['directory'])
            if not handle:
                break
            try:
                with tasks.execution_scope(handle):
                    result = process_navigation(handle['payload'])
            except tasks.TaskDeferred as deferred:
                tasks.handoff(handle, deferred)
                job = db.session.get(BackgroundTask, handle['id'])
                job.available_at = datetime.utcnow()
                db.session.commit()
            else:
                tasks.finish(handle, result)
                completed.append(result)
        return completed

    def test_three_pages_share_two_empty_windows_and_keep_every_undecided_route(self):
        jobs = self.navigation_jobs()
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs:
                   navigation_fixture.reply(args[2], role='gateway')) as model:
            results = self.finish_navigation()
        self.assertEqual(model.call_count, 8)
        self.assertTrue(any(result['status'] == 'branch_stopped' for result in results))
        self.assertTrue(any(result.get('remaining_routes', 0) for result in results))
        for job in jobs:
            db.session.refresh(job)
            self.assertEqual(job.state, 'done')
        with self.store.connect() as connection:
            pending = connection.execute("SELECT count(*) FROM edges WHERE site_key=? AND decision='navigation_pending'",
                                         (self.key,)).fetchone()[0]
        self.assertGreater(pending, 0)

    def branch(self, url, scope='fixture-generation-1'):
        from backend.scraper.discovery.navigation_progress import NavigationBranch
        return NavigationBranch(self.store, self.key, self.fixture.school_id, scope, url)

    def stop(self, branch):
        with branch.lock:
            branch.state()
            for index in range(8):
                state = branch.state(decision_id=f'fixture:{index}')
        self.assertTrue(state['stopped'])
        return state

    def test_stopped_clock_survives_restart_and_only_own_new_material_resumes_it(self):
        local = ROOT + 'library/current/'
        self.page(local, 'channel', 2)
        self.edges(ROOT + 'library/', [local])
        branch = self.branch(local)
        self.stop(branch)
        restarted = self.branch(ROOT + 'library/')
        self.assertTrue(restarted.state()['stopped'])
        self.page(ROOT + 'office/new/', 'channel', 2)
        self.edges(ROOT + 'office/', [ROOT + 'office/new/'])
        self.assertTrue(restarted.state()['stopped'])
        new_local = ROOT + 'library/new/'
        self.page(new_local, 'channel', 2)
        self.edges(ROOT + 'library/', [local, new_local])
        resumed = restarted.state()
        self.assertFalse(resumed['stopped'])
        self.assertEqual(resumed['decisions'], 8)
        self.assertEqual(resumed['no_change_checks'], 0)
        # Replaying a previous paid ID cannot advance this clock.
        self.assertEqual(restarted.state(decision_id='fixture:7')['decisions'], 8)

    def test_full_cache_cleanup_keeps_the_durable_branch_clock(self):
        branch = self.branch(ROOT + 'library/')
        original = self.stop(branch)
        self.store.trim(all_cache=True)
        reopened = self.branch(ROOT + 'library/')
        self.assertEqual(reopened.state(), original)
        self.assertIsNotNone(self.store.get_page(self.key, ROOT + 'library/'))

    def test_new_reads_before_a_check_are_counted_even_when_seen_was_already_merged(self):
        branch = self.branch(ROOT + 'library/')
        branch.state()
        for index in range(4):
            state = branch.state(decision_id=f'growth:{index}')
        self.assertEqual(state['no_change_checks'], 1)
        local = ROOT + 'library/new-growth/'
        self.page(local, 'channel', 2)
        self.edges(ROOT + 'library/', [local])
        branch.state()  # The pre-dispatch refresh merges observed IDs first.
        for index in range(4, 8):
            state = branch.state(decision_id=f'growth:{index}')
        self.assertFalse(state['stopped'])
        self.assertEqual(state['no_change_checks'], 0)

    def test_real_unit_child_and_ambiguous_cross_branch_child_keep_independent_clocks(self):
        unit = ROOT + 'library/subunit/'
        self.page(unit, 'unit', 2)
        self.edges(ROOT + 'library/', [unit])
        parent = self.branch(ROOT + 'library/')
        self.stop(parent)
        self.assertFalse(self.branch(unit).state()['stopped'])
        self.assertNotEqual(parent.url, self.branch(unit).url)
        ambiguous = ROOT + 'shared-channel/'
        self.page(ambiguous, 'channel', 3)
        self.edges(unit, [ambiguous])
        self.edges(ROOT + 'office/', [ambiguous])
        self.assertEqual(self.branch(ambiguous).url, ambiguous)
        self.assertFalse(self.branch(ambiguous).state()['stopped'])
        # The same branch in a new explicit generation has its own clock.
        self.assertFalse(self.branch(ROOT + 'library/', scope='fixture-generation-2').state()['stopped'])

    def test_reclassified_unit_does_not_import_its_old_shared_parent_clock(self):
        child = ROOT + 'library/reclassified/'
        self.page(child, 'gateway', 2)
        self.edges(ROOT + 'library/', [child])
        original = self.branch(child)
        old_state = self.stop(original)
        self.store.enqueue(self.key, child, '实际独立单位', 'unit', 2, [], 'school_domain')
        independent = self.branch(child)
        self.assertNotEqual(original.url, independent.url)
        fresh = independent.state(previous=old_state, previous_branch_url=original.url)
        self.assertFalse(fresh['stopped'])
        self.assertEqual(fresh['decisions'], 0)

    def test_reclassified_independent_unit_reopens_after_reading_identical_html(self):
        from backend.scraper.discovery.navigation_progress import resume_stopped_navigation
        job = self.navigation_jobs(count=1)[0]
        parent = db.session.get(BackgroundTask, job.payload['parent_task_id'])
        page = job.payload['page']
        old_branch = self.branch(page['url'], f'{self.fixture.school_id}:{parent.id}:{parent.generation}')
        old_state = self.stop(old_branch)
        job.state = 'done'
        job.result = {'status': 'branch_stopped'}
        job.checkpoint = {'navigation_progress': old_state, 'navigation_branch_url': old_branch.url,
                          'route_cursor': 8}
        db.session.commit()
        html = self.store.snapshot(self.key, page['url'])
        self.store.enqueue(self.key, page['url'], '独立单位', 'unit', page['depth'], [], 'school_domain')
        self.store.finish(self.key, page['url'], state='fetched', html=html)
        self.assertEqual(resume_stopped_navigation(self.store, self.key, self.fixture.school_id), 1)
        db.session.refresh(job)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.checkpoint['route_cursor'], 8)
        self.assertEqual(job.checkpoint['navigation_progress']['decisions'], 0)
        self.assertFalse(job.checkpoint['navigation_progress']['stopped'])

    def test_stale_or_reference_only_edges_do_not_assign_branch_progress(self):
        child = ROOT + 'library/unread-parent-reference/'
        self.page(child, 'channel', 2)
        self.edges(ROOT + 'library/', [child], decision='route_reference')
        self.assertEqual(self.branch(child).url, child)
        self.edges(ROOT + 'library/', [child])
        self.assertEqual(self.branch(child).url, ROOT + 'library/')
        self.store.finish(self.key, ROOT + 'library/', state='fetched', html='<main>新材料</main>')
        self.assertEqual(self.branch(child).url, child)

    def test_stopped_branch_applies_all_free_cache_batches_without_a_model_call(self):
        from backend.ai.skill_loader import load_skill
        from backend.scraper.discovery.layered import navigation_candidates, route_structure
        from backend.scraper.discovery.navigation_roles import candidate_key, save
        from backend.scraper.discovery.structure import extract_structure
        jobs = self.navigation_jobs(count=1)
        page = jobs[0].payload['page']
        html = self.store.snapshot(self.key, page['url'])
        parsed = route_structure(extract_structure(html, page['url'], ROOT, page['kind'], page['label'], []),
                                 page, html, ROOT, policy='valuable')
        binding = {'id': 1, 'version': 1}
        skill_digest = load_skill('student-information', 'navigation').resource_digest
        site = self.store.report(self.key)['site']
        rows = []
        for link, _ in navigation_candidates(html, parsed, page):
            key = candidate_key(site, link, link['_navigation_material'], binding, skill_digest, 'student-routes-2')
            rows.append((key, {'role': 'gateway', 'value': 'potential', 'facts': [{'quote': link['label']}]}))
        save(rows)
        parent = db.session.get(BackgroundTask, jobs[0].payload['parent_task_id'])
        scope = f'{self.fixture.school_id}:{parent.id}:{parent.generation}'
        self.stop(self.branch(page['url'], scope))
        with patch('backend.ai.configuration.get_model_binding', return_value=binding), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('cached roles are free')) as model:
            results = self.finish_navigation()
        model.assert_not_called()
        self.assertEqual(results[-1]['status'], 'processed')
        self.assertEqual(results[-1]['reviewed_routes'], 32)

    def test_new_successful_http_read_reopens_stopped_work_immediately(self):
        from backend.scraper.discovery.inventory_crawler import inspect_page
        jobs = self.navigation_jobs()
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs:
                   navigation_fixture.reply(args[2], role='gateway')) as model:
            self.finish_navigation()
            self.assertEqual(model.call_count, 8)
            stopped = [job for job in jobs if (db.session.get(BackgroundTask, job.id).result or {}).get('status') == 'branch_stopped']
            self.assertTrue(stopped)
            cursors = {job.id: job.checkpoint['route_cursor'] for job in stopped}
            from datetime import timedelta
            for job in stopped:
                job.deadline_at = datetime.utcnow() - timedelta(hours=3)
            db.session.commit()
            url = ROOT + 'library/new-http-evidence/'
            self.store.enqueue(self.key, url, '新观察材料', 'channel', 2, [], 'school_domain')
            self.edges(ROOT + 'library/', [job.payload['page']['url'] for job in jobs] + [url])
            handle = tasks.claim(capabilities=['http'])
            self.assertIsNotNone(handle)
            with tasks.execution_scope(handle):
                inspect_page(self.store, self.store.report(self.key)['site'], self.store.get_page(self.key, url),
                             fetcher=lambda target: {'url': target, 'html': '<main>新的实读官网材料</main>', 'status': 200},
                             focus='valuable')
            tasks.finish(handle, {})
            for job in stopped:
                db.session.refresh(job)
                self.assertEqual(job.state, 'pending')
                self.assertEqual(job.checkpoint['route_cursor'], cursors[job.id])
                self.assertGreater(job.deadline_at, datetime.utcnow())
            results = self.finish_navigation()
        self.assertTrue(all(result['status'] == 'processed' for result in results))
        self.assertEqual(model.call_count, 12)

    def test_stopped_branch_preserves_existing_execution_states_for_runtime_recovery(self):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import load_skill
        branch = self.branch(ROOT + 'library/')
        self.stop(branch)
        skill = load_skill('student-information', 'navigation')
        row = AIExecution(execution_id='routes:already-dispatched', purpose='directory', profile_id=None,
            config_version='1', provider='fixture', model='fixture', skill_id=skill.id,
            skill_version=skill.version, skill_digest=skill.resource_digest, input_digest='exact-original-input',
            mode='navigation', status='reserved', request_sent=False, usage={'known': False})
        db.session.add(row)
        db.session.commit()
        for status in ('reserved', 'sending', 'uncertain', 'succeeded', 'failed'):
            row.status = status
            row.request_sent = status != 'reserved'
            db.session.commit()
            self.assertTrue(branch.allows(row.execution_id), status)
        self.assertFalse(branch.allows('routes:unbought'))

    def test_http_progress_does_not_release_user_budget_or_unknown_result_pauses(self):
        from backend.ai.models import AIBudget, AISchoolRound
        from backend.scraper.discovery.navigation_progress import resume_stopped_navigation
        from backend.services.discovery_control import PAUSE_KEY
        jobs = self.navigation_jobs()
        parent = db.session.get(BackgroundTask, jobs[0].payload['parent_task_id'])
        scope = f'{self.fixture.school_id}:{parent.id}:{parent.generation}'
        state = self.stop(self.branch(jobs[0].payload['page']['url'], scope))
        for job, phase in zip(jobs, ('ai_budget', 'ai_result', 'complete')):
            job.state = 'done' if phase == 'complete' else 'waiting'
            job.phase = phase
            job.result = {'status': 'branch_stopped'}
            job.checkpoint = dict(job.checkpoint or {}, navigation_progress=state,
                                  navigation_branch_url=ROOT + 'library/')
        parent.payload = dict(parent.payload or {}, **{PAUSE_KEY: True})
        db.session.commit()
        local = ROOT + 'library/pause-test-new/'
        self.page(local, 'channel', 2)
        self.edges(ROOT + 'library/', [job.payload['page']['url'] for job in jobs] + [local])
        before_budgets = (AIBudget.query.count(), AISchoolRound.query.count())
        self.assertEqual(resume_stopped_navigation(self.store, self.key, self.fixture.school_id), 0)
        for job, phase in zip(jobs, ('ai_budget', 'ai_result', 'complete')):
            db.session.refresh(job)
            self.assertEqual(job.phase, phase)
            self.assertEqual(job.state, 'done' if phase == 'complete' else 'waiting')
            self.assertEqual(job.checkpoint['navigation_progress'], state)
        self.assertEqual((AIBudget.query.count(), AISchoolRound.query.count()), before_budgets)

    def test_unknown_original_plan_stays_waiting_after_branch_stop_and_restart(self):
        import copy
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest, load_skill
        jobs = self.navigation_jobs(count=1)
        skill = load_skill('student-information', 'navigation')

        def unknown(*args, **kwargs):
            db.session.add(AIExecution(execution_id=args[4], purpose='directory', profile_id=None,
                config_version='1', provider='fixture', model='fixture', skill_id=skill.id,
                skill_version=skill.version, skill_digest=skill.resource_digest,
                input_digest=digest({'evidence': args[2], 'repair_feedback': None}), mode='navigation',
                status='uncertain', request_sent=True, usage={'known': False}, error_code='connection_lost'))
            db.session.commit()
            return {'status': 'uncertain', 'error_code': 'connection_lost'}

        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': None, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=unknown):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                process_navigation(handle['payload'])
        self.assertEqual(waiting.exception.phase, 'ai_result')
        tasks.handoff(handle, waiting.exception)
        job = jobs[0]
        db.session.refresh(job)
        original = copy.deepcopy(job.checkpoint['route_pending_decision'])
        parent = db.session.get(BackgroundTask, job.payload['parent_task_id'])
        self.stop(self.branch(job.payload['page']['url'], f'{self.fixture.school_id}:{parent.id}:{parent.generation}'))
        job.state = 'pending'
        job.available_at = datetime.utcnow()
        db.session.commit()
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), patch('backend.ai.runtime.run_skill') as model:
            with self.assertRaises(tasks.TaskDeferred) as resumed:
                process_navigation(handle['payload'])
        model.assert_not_called()
        self.assertEqual(resumed.exception.phase, 'ai_result')
        tasks.handoff(handle, resumed.exception)
        db.session.refresh(job)
        self.assertEqual(job.checkpoint['route_pending_decision'], original)
        self.assertEqual(AIExecution.query.count(), 1)
        self.assertEqual(job.state, 'waiting')

    def test_post_model_writer_contention_does_not_lose_a_paid_decision_from_the_clock(self):
        from filelock import FileLock, Timeout
        from backend.scraper.discovery import navigation_progress as progress_module
        self.navigation_jobs(count=1, items=100)
        locks = 0

        def writer(*args, **kwargs):
            nonlocal locks
            if str(args[0]).endswith('.worker.lock'):
                locks += 1
                if locks == 2:
                    raise Timeout('fixture-post-model-writer')
            return FileLock(*args, **kwargs)

        with patch('filelock.FileLock', side_effect=writer), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs:
                   navigation_fixture.reply(args[2], role='gateway')) as model:
            results = self.finish_navigation()
        self.assertEqual(model.call_count, 8)
        self.assertEqual(results[-1]['status'], 'branch_stopped')

    def test_crossing_stop_threshold_still_applies_remaining_free_cache_batch(self):
        from backend.ai.skill_loader import load_skill
        from backend.scraper.discovery.layered import navigation_candidates, route_structure
        from backend.scraper.discovery.navigation_roles import candidate_key, save
        from backend.scraper.discovery.structure import extract_structure
        jobs = self.navigation_jobs(count=1, items=72)
        page = jobs[0].payload['page']
        html = self.store.snapshot(self.key, page['url'])
        parsed = route_structure(extract_structure(html, page['url'], ROOT, page['kind'], page['label'], []),
                                 page, html, ROOT, policy='valuable')
        binding = {'id': 1, 'version': 1}
        skill_digest = load_skill('student-information', 'navigation').resource_digest
        site = self.store.report(self.key)['site']
        rows = []
        for link, _ in navigation_candidates(html, parsed, page)[-8:]:
            key = candidate_key(site, link, link['_navigation_material'], binding, skill_digest, 'student-routes-2')
            rows.append((key, {'role': 'gateway', 'value': 'potential', 'facts': [{'quote': link['label']}]}))
        save(rows)
        with patch('backend.ai.configuration.get_model_binding', return_value=binding), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs:
                   navigation_fixture.reply(args[2], role='gateway')) as model:
            results = self.finish_navigation()
        self.assertEqual(model.call_count, 8)
        self.assertEqual(results[-1]['status'], 'processed')
        self.assertEqual(results[-1]['reviewed_routes'], 72)


if __name__ == '__main__':
    unittest.main()
