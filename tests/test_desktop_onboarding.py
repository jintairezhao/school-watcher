"""First-run and frozen-resource regressions, with no user data or external calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class FrozenResourcesTests(unittest.TestCase):
    def test_frozen_parser_uses_build_revision_without_source_files(self):
        from backend.scraper.discovery import parser_revision
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config').mkdir()
            (root / 'config/parser-revision.txt').write_text('a' * 64, encoding='ascii')
            with patch.object(sys, 'frozen', True, create=True), patch.object(parser_revision, 'ROOT', root):
                self.assertEqual(parser_revision.current_revision(), 'a' * 64)

    def test_resources_include_ai_contracts_but_not_developer_school_seeds(self):
        from desktop.resources import application_data
        with tempfile.TemporaryDirectory() as folder:
            data = application_data(ROOT, Path(folder))
            paths = {str(Path(dest) / Path(source).name).replace('\\', '/') for source, dest in data}
            self.assertIn('config/parser-revision.txt', paths)
            self.assertIn('backend/ai/skills/university-source-onboarding/manifest.json', paths)
            self.assertIn('backend/ai/skills/summarize-university-notice/SKILL.md', paths)
            self.assertNotIn('config/desktop-schools.json', paths)
            self.assertNotIn('config/schools.yaml', paths)


class FirstSubscriptionTests(unittest.TestCase):
    def setUp(self):
        from test_desktop_local import DesktopLocalTests
        self.fixture = DesktopLocalTests()
        self.fixture.setUp()
        self.fixture.open()
        self.client = self.fixture.client
        from backend.services.school_registry import ensure_school
        self.school, _ = ensure_school('上海大学', 'https://www.shu.edu.cn/')
        from backend.routes.subscriptions import subscribe_school
        from backend.auth.desktop import ensure_local_owner
        subscribe_school(self.school, ensure_local_owner().id)
        with self.client.session_transaction() as session:
            self.headers = {'X-CSRF-Token': session['_csrf_token']}

    def tearDown(self):
        self.fixture.tearDown()

    def test_first_subscription_queues_ai_and_missing_configuration_is_actionable(self):
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        from backend.worker import dispatch
        row = BackgroundTask.query.filter_by(identity=f'discover:{self.school.id}').one()
        self.assertTrue(row.payload['ai_assist'])
        self.assertTrue(row.payload['require_ai'])
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), self.assertRaises(tasks.TaskDeferred) as raised:
            dispatch('discover', handle['payload'])
        tasks.handoff(handle, raised.exception)
        result = self.client.get(f'/api/subscriptions/{self.school.id}/discovery')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['state'], 'waiting')
        self.assertFalse(result.json['needs_verification'])
        self.assertIn('配置 AI', result.json['message'])
        page = self.client.get(f'/subscriptions/{self.school.id}')
        self.assertIn('配置 AI 并继续', page.text)
        self.assertNotIn('正在等待首次同步', page.text)

    def test_configured_ai_resumes_waiting_job_without_duplicate_work(self):
        from backend.database.db import db
        from backend.database.models import BackgroundTask
        row = BackgroundTask.query.filter_by(kind='discover').one()
        row.state, row.phase = 'waiting', 'ai_setup'
        db.session.commit()
        with patch('backend.services.onboarding_progress.ai_available', return_value=True):
            response = self.client.post(f'/api/subscriptions/{self.school.id}/discovery', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        db.session.refresh(row)
        self.assertEqual(row.state, 'pending')
        self.assertEqual(BackgroundTask.query.filter_by(kind='discover').count(), 1)

    def test_failure_and_live_counts_are_visible_without_exposing_task_secrets(self):
        from backend.database.db import db
        from backend.database.models import BackgroundTask
        row = BackgroundTask.query.filter_by(kind='discover').one()
        row.state = 'failed'
        row.error = 'sensitive local diagnostic path'
        row.checkpoint = {'discovery_progress': {'checked_pages': 8, 'pending_pages': 14, 'phase': 'crawl'}}
        db.session.commit()
        response = self.client.get(f'/api/subscriptions/{self.school.id}/discovery')
        self.assertEqual(response.json['state'], 'failed')
        self.assertEqual(response.json['checked_pages'], 8)
        self.assertTrue(response.json['can_retry'])
        self.assertNotIn('sensitive local', response.text)

    def test_unsubscribed_school_progress_is_private(self):
        from backend.services.school_registry import ensure_school
        other, _ = ensure_school('其他学校', 'https://other.edu.cn/')
        self.assertEqual(self.client.get(f'/api/subscriptions/{other.id}/discovery').status_code, 403)
        self.assertEqual(self.client.post(f'/api/subscriptions/{other.id}/discovery', headers=self.headers).status_code, 403)

    def test_publication_month_is_validated_and_backfill_resets_incremental_cursor(self):
        from datetime import datetime
        from backend.database.db import db
        from backend.database.models import AppConfig, Department
        department = Department(school_id=self.school.id, name='通知', list_url=self.school.url,
                                last_scraped_at=datetime.utcnow())
        db.session.add(department); db.session.commit()
        month = f'{datetime.now().year - 1}-06'
        response = self.client.post('/api/settings', json={'since_month': month, 'interval': 15}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        db.session.refresh(department)
        self.assertIsNone(department.last_scraped_at)
        self.assertEqual(self.client.get('/api/admin/stats').json['scrape_since_month'], month)
        for invalid in ('2026-00', '9999-01', '2026-6', '../data', 2024, None):
            response = self.client.post('/api/settings', json={'since_month': invalid, 'interval': 10}, headers=self.headers)
            self.assertEqual(response.status_code, 400, invalid)
            self.assertEqual(AppConfig.get('scrape_interval'), '15', 'invalid settings must not partially persist')

    def test_month_filter_keeps_boundary_and_undated_items_and_stops_old_pages(self):
        from datetime import datetime
        from backend.database.db import db
        from backend.database.models import AppConfig, Department, Announcement
        from backend.scraper.engine import scrape_department
        year = datetime.now().year - 1
        AppConfig.set('scrape_since_month', f'{year}-06')
        department = Department(school_id=self.school.id, name='通知', list_url=self.school.url,
                                list_selector='li', title_selector='a', link_selector='a', date_selector='time')
        db.session.add(department); db.session.commit()
        def item(name, stamp, link):
            return f'<li><a href="{link}">{name}</a><time>{stamp}</time></li>'
        first = item('开学注册安排通知', f'{year}-06-01', '/new') + item('长期办理事项相关通知', '', '/undated')
        first += ''.join(item(f'历史通知事项第 {i} 项', f'{year}-05-31', f'/old-{i}') for i in range(4))
        first += f'<li><a href="/{year}/05/30/expired.html">地址标注日期的历史通知</a></li>'
        first += '<a href="/page2">下一页</a>'
        old = item('历史通知事项说明', f'{year}-05-31', '/older')
        pages = ['<ul>' + first + '</ul>', '<ul>' + old + '</ul><a href="/page3">下一页</a>',
                 '<ul>' + old + '</ul><a href="/page4">下一页</a>']
        with patch('backend.scraper.engine._fetch_html', side_effect=pages) as fetch, \
                patch('backend.scraper.engine.save_element_signatures'), \
                patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action': 'healthy'}):
            new, total = scrape_department(department, self.school.url)
        self.assertEqual(fetch.call_count, 3, 'confirm the boundary, then skip older pages')
        self.assertEqual(new, 2)
        self.assertEqual({a.title for a in Announcement.query.all()}, {'开学注册安排通知', '长期办理事项相关通知'})

    def test_daily_health_reuses_recent_collection_but_fetches_stale_columns(self):
        from datetime import datetime, timedelta
        from backend.database.db import db
        from backend.database.models import Department
        from backend.worker import dispatch
        department = Department(school_id=self.school.id, name='通知', list_url=self.school.url,
                                list_selector='li', last_scraped_at=datetime.utcnow())
        db.session.add(department); db.session.commit()
        payload = {'school_id': self.school.id, 'department_id': department.id}
        with patch('backend.scraper.engine._fetch_html', return_value='<ul></ul>') as fetch, \
                patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action': 'healthy'}):
            self.assertTrue(dispatch('source_health', payload)['reused_recent_collection'])
            fetch.assert_not_called()
            department.last_scraped_at = datetime.utcnow() - timedelta(hours=1)
            db.session.commit()
            self.assertEqual(dispatch('source_health', payload)['checked'], 1)
            self.assertEqual(fetch.call_count, 1)

    def test_backfill_resumes_after_three_pages_and_repairs_legacy_truncated_sync(self):
        from datetime import datetime
        from backend.database.db import db
        from backend.database.models import Department, Announcement, AppConfig
        from backend.services import tasks
        from backend.scraper.engine import scrape_department
        from backend.services.collection_settings import coverage_complete, since_date
        from backend.services.announcement_identity import upsert_listing
        year = datetime.now().year - 1
        AppConfig.set('scrape_since_month', f'{year}-01')
        department = Department(school_id=self.school.id, name='通知', list_url=self.school.url + 'notices/',
                                list_selector='li', title_selector='a', link_selector='a', date_selector='time',
                                last_scraped_at=datetime.utcnow())
        db.session.add(department); db.session.commit()
        # v0.2.1 set last_scraped_at after only three pages. Existing entries
        # cannot imply that the requested historical range was ever covered.
        for number in range(1, 4):
            upsert_listing(department, f'旧版已抓取事项通知 {number}', self.school.url + f'notice-{number}')
        db.session.commit()
        requested = []
        def fetch(url, **kwargs):
            requested.append(url)
            number = 1 if url.endswith('notices/') else int(url.rsplit('/', 1)[1])
            next_link = f'<a href="/page/{number + 1}">下一页</a>' if number < 8 else ''
            return f'<ul><li><a href="/notice-{number}">学校历史事项相关通知 {number}</a><time>{year}-06-01</time></li></ul>{next_link}'
        task = tasks.enqueue('collect', department.id, {'school_id': self.school.id, 'department_id': department.id})
        with patch('backend.scraper.engine._fetch_html', side_effect=fetch), \
                patch('backend.scraper.engine.save_element_signatures'), \
                patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action': 'healthy'}):
            for attempt in range(4):
                handle = tasks.claim(capabilities=['http'])
                # Ignore the subscription's initial scheduling task in this fixture.
                if handle['kind'] != 'collect':
                    tasks.finish(handle, result={'skipped': True})
                    handle = tasks.claim(capabilities=['http'])
                try:
                    with tasks.execution_scope(handle):
                        result = scrape_department(department, self.school.url)
                except tasks.TaskDeferred as deferred:
                    self.assertEqual(deferred.phase, 'pagination')
                    tasks.handoff(handle, deferred)
                    task.available_at = datetime.utcnow()
                    db.session.commit()
                    self.assertFalse(coverage_complete(department, since_date()))
                else:
                    tasks.finish(handle, result={'new_count': result[0]})
                    break
            else:
                self.fail('historical backfill did not finish')
        self.assertEqual(len(requested), 8)
        self.assertEqual(len(set(requested)), 8, 'completed pages must not be re-fetched on continuation')
        self.assertEqual(Announcement.query.count(), 8)
        self.assertTrue(coverage_complete(department, since_date()))

    def test_unused_legacy_presets_are_hidden_but_selected_school_survives(self):
        from backend.services.school_registry import ensure_school
        from backend.services.starter_catalog import managed_schools
        from backend.routes.subscriptions import subscribe_school
        from backend.auth.desktop import ensure_local_owner
        old, _ = ensure_school('旧版预设', 'https://legacy.edu.cn/', origin='desktop-catalog')
        self.assertNotIn(old.id, [s.id for s in managed_schools()])
        subscribe_school(old, ensure_local_owner().id)
        self.assertIn(old.id, [s.id for s in managed_schools()])

    def test_ai_uses_observed_first_page_links_and_does_not_activate_sources(self):
        from backend.services import tasks
        from backend.scraper.discovery.ai_navigation import assist_navigation
        from backend.database.models import Department
        handle = tasks.claim(capabilities=['directory'])
        page = {'url': self.school.url, 'kind': 'root', 'depth': 0, 'label': '上海大学', 'path_json': '[]'}
        site = {'root_url': self.school.url, 'name': self.school.name, 'site_key': 'shu'}
        parsed = {'links': []}
        output = {'status': 'succeeded', 'output': {'results': [
            {'candidate_id': 'link-0', 'kind': 'directory', 'decision': 'propose'},
            {'candidate_id': 'invented', 'kind': 'channel', 'decision': 'propose'}]}}
        with tasks.execution_scope(handle), patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
                patch('backend.ai.runtime.run_skill', return_value=output) as run:
            assist_navigation(site, page, '<a href="/units/">机构设置</a><a href="http://127.0.0.1/">内网</a>', parsed)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(run.call_args.args[2]['candidates']), 1)
            self.assertEqual(parsed['links'][0]['url'], 'https://www.shu.edu.cn/units/')
            self.assertEqual(len(parsed['links']), 1)
            assist_navigation(site, page, '<a href="/units/">机构设置</a>', parsed)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(Department.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
