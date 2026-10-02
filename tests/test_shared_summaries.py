"""Shared summaries: real isolated DB races and content/ownership fences; no paid calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department, School, User
from backend.ai.summary_models import AnnouncementSummary
from backend.services import summaries, tasks

BINDING = {'id': 0, 'version': 1, 'provider': 'deepseek', 'model': 'test', 'region': 'default',
           'max_output_tokens': 4096}
BODY = '请符合条件的同学提交申请材料。截止时间为2026年10月1日，逾期不受理。'


def response(evidence, text='请符合条件的同学于2026年10月1日前提交申请材料。'):
    paragraphs = evidence['paragraphs']
    return {'status': 'succeeded', 'output': {'summary': text, 'facts': [
        {'kind': 'action', 'text': p['text'], 'evidence_ids': [p['id']]} for p in paragraphs if p['text']],
        'coverage': {'paragraph_ids': [p['id'] for p in paragraphs], 'attachments_included': False}},
        'provenance': {'skill_version': '1.0.0'}, 'usage': {}}


class SharedSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watcher-summaries-')
        root = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'summary-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(root / 'main.db'),
            'SOURCE_CATALOG_PATH': str(root / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(root / 'discovery.db')})
        if 'summaries' not in self.app.blueprints:
            from backend.routes.summaries import bp
            self.app.register_blueprint(bp)
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        school = School(name='测试学校', url='https://example.edu.cn', enabled=True)
        self.reader = User(username='reader', password_hash='unused')
        self.admin = User(username='admin', password_hash='unused', role='admin')
        db.session.add_all([school, self.reader, self.admin]); db.session.flush()
        dept = Department(school_id=school.id, name='学院通知', list_url=school.url)
        db.session.add(dept); db.session.flush()
        self.ann = Announcement(school_id=school.id, department_id=dept.id, title='申请通知',
            url=school.url + '/notice/1', content_text=BODY, content_cached_at=datetime.utcnow())
        db.session.add(self.ann); db.session.commit()
        self.ann_id = self.ann.id
        self.binding = patch('backend.ai.configuration.get_model_binding', return_value=dict(BINDING))
        self.binding.start()

    def tearDown(self):
        self.binding.stop()
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def client(self, user=None):
        client = self.app.test_client()
        if user:
            with client.session_transaction() as session:
                session.update(user_id=user.id, _csrf_token='token')
        return client

    def request(self, force=False):
        result = summaries.request_summary(db.session.get(Announcement, self.ann_id),
                                          requested_by=self.reader.id, force=force)
        return db.session.get(AnnouncementSummary, result['id'])

    def run_row(self, row, fake=None):
        def success(row_id, mode, evidence, stage):
            return response(evidence)
        with patch('backend.services.summaries._call', side_effect=fake or success):
            return summaries.generate_summary(row.id)

    def test_hundred_requests_share_one_record_and_task(self):
        user_id = self.reader.id
        db.session.remove()
        def request(_):
            with self.app.app_context():
                ann = db.session.get(Announcement, self.ann_id)
                result = summaries.request_summary(ann, requested_by=user_id)
                db.session.remove()
                return result['id'], result['task_id']
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(request, range(100)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(AnnouncementSummary.query.count(), 1)
        self.assertEqual(BackgroundTask.query.filter_by(kind='summary').count(), 1)

    def test_passive_reads_never_enqueue_and_generate_requires_auth(self):
        with patch('backend.services.summaries.request_summary', side_effect=AssertionError('paid')):
            result = self.client(self.reader).get(f'/api/announcements/{self.ann_id}/summary')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(BackgroundTask.query.count(), 0)
        self.assertEqual(result.get_json()['status'], 'none')
        result = self.client().post(f'/api/announcements/{self.ann_id}/summary', json={})
        self.assertIn(result.status_code, (401, 403))

    def test_force_and_batch_are_admin_only_and_visibility_is_checked(self):
        client = self.client(self.reader)
        headers = {'X-CSRF-Token': 'token'}
        self.assertEqual(client.post(f'/api/announcements/{self.ann_id}/summary',
            json={'force': True}, headers=headers).status_code, 403)
        self.assertEqual(client.post('/api/summaries/batch', json={'ids': [self.ann_id]},
                                    headers=headers).status_code, 403)
        self.ann.school.enabled = False; db.session.commit()
        self.assertEqual(client.get(f'/api/announcements/{self.ann_id}/summary').status_code, 404)
        self.assertEqual(client.post(f'/api/announcements/{self.ann_id}/summary',
                                    json={}, headers=headers).status_code, 404)

    def test_success_is_shared_cached_without_config_or_key(self):
        row = self.request()
        self.run_row(row)
        self.assertEqual(summaries.current_summary(self.ann), '请符合条件的同学于2026年10月1日前提交申请材料。')
        with patch('backend.ai.configuration.get_model_binding', side_effect=AssertionError('no config needed')):
            result = summaries.request_summary(self.ann)
        self.assertEqual(result['status'], 'succeeded')
        projected = db.session.query(summaries.summary_expression()).select_from(Announcement).scalar()
        self.assertEqual(projected, result['summary'])
        self.assertFalse(self.ann.summary)

    def test_late_result_cannot_overwrite_changed_tail(self):
        self.ann.content_text = '正文开头。' * 300 + BODY
        db.session.commit()
        row = self.request()
        def late(row_id, mode, evidence, stage):
            ann = db.session.get(Announcement, self.ann_id)
            ann.content_text += '尾部新增：截止时间改为2026年9月30日。'
            db.session.commit()
            return response(evidence)
        result = self.run_row(row, late)
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(summaries.current_summary(self.ann), '')
        self.assertEqual(AnnouncementSummary.query.filter_by(state='succeeded').count(), 0)

    def test_legacy_summary_is_history_only(self):
        self.ann.summary = '旧的无版本摘要'; db.session.commit()
        result = summaries.summary_status(self.ann)
        self.assertEqual(result['summary'], '')
        self.assertEqual(result['historical'][0]['summary'], '旧的无版本摘要')
        self.assertEqual(summaries.current_summary(self.ann), '')

    def test_missing_body_hands_off_to_shared_content_job_without_retry(self):
        self.ann.content_text = ''; self.ann.content_cached_at = None; db.session.commit()
        row = self.request()
        with self.assertRaises(tasks.TaskDeferred):
            summaries.generate_summary(row.id)
        with self.assertRaises(tasks.TaskDeferred):
            summaries.generate_summary(row.id)
        self.assertEqual(BackgroundTask.query.filter_by(kind='content').count(), 1)
        self.assertEqual(db.session.get(BackgroundTask, row.task_id).attempts, 0)

    def test_longbody_keeps_tail_and_all_paragraphs_through_synthesis(self):
        self.ann.content_text = '背景说明。' * 2000 + '\n最后办理期限为2026年10月1日。'
        db.session.commit()
        row = self.request()
        seen = []
        def chunks(row_id, mode, evidence, stage):
            seen.append((mode, evidence))
            return response(evidence)
        self.assertEqual(self.run_row(row, chunks)['status'], 'succeeded')
        self.assertGreater(len(seen), 2)
        self.assertEqual(seen[-1][0], 'synthesize')
        extracted = '\n'.join(p['text'] for mode, evidence in seen if mode == 'extract'
                              for p in evidence['paragraphs'])
        self.assertIn('最后办理期限为2026年10月1日。', extracted)
        self.assertEqual(row.input_scope['characters'], len(self.ann.content_text))

    def test_failure_placeholders_and_false_coverage_do_not_become_summary(self):
        row = self.request()
        def false_coverage(row_id, mode, evidence, stage):
            result = response(evidence)
            result['output']['coverage']['paragraph_ids'] = ['invented']
            return result
        self.assertEqual(self.run_row(row, false_coverage)['status'], 'failed')
        self.assertEqual(row.summary, '')

    def test_expired_worker_cannot_commit_summary(self):
        row = self.request()
        handle = tasks.claim()
        task = db.session.get(BackgroundTask, handle['id'])
        task.lease_until = datetime.utcnow() - timedelta(seconds=1); db.session.commit()
        with tasks.execution_scope(handle), self.assertRaises(tasks.LeaseLost):
            self.run_row(row)
        self.assertEqual(AnnouncementSummary.query.filter_by(state='succeeded').count(), 0)

    def test_short_and_oversized_bodies_have_explicit_non_success_status(self):
        self.ann.content_text = '极短正文'; db.session.commit()
        row = self.request()
        self.assertEqual(summaries.generate_summary(row.id)['status'], 'not_needed')
        self.ann.content_text = '正文' * 65000; db.session.commit()
        row = self.request()
        result = summaries.generate_summary(row.id)
        self.assertEqual(result['error_code'], 'content_too_large')
        self.assertFalse(result['summary'])

    def test_batch_requires_explicit_bounded_scope(self):
        for invalid in (None, [], [True], [0], list(range(1, 52))):
            with self.assertRaises(ValueError):
                summaries.enqueue_batch(invalid)

    def test_runtime_integration_no_transaction_at_provider_and_replay_no_charge(self):
        from backend.ai.providers import ProviderResult
        from backend.ai.models import AIExecution
        row = self.request()
        def provider(binding, key, messages, **kwargs):
            self.assertFalse(db.session().in_transaction())
            evidence = {'paragraphs': summaries._paragraphs(BODY)}
            return ProviderResult(json.dumps(response(evidence)['output'], ensure_ascii=False), 'stop',
                                  {'known': True, 'total_tokens': 30}, 'fixture-request')
        with patch('backend.ai.runtime.credential_for', return_value='fixture-only'), \
             patch('backend.ai.providers.complete', side_effect=provider) as paid:
            result = summaries.generate_summary(row.id)
            self.assertEqual(result['status'], 'succeeded', result)
            summaries.generate_summary(row.id)
            self.assertEqual(paid.call_count, 1)
        self.assertEqual(AIExecution.query.count(), 1)

    def test_invalid_json_repairs_at_most_once_and_unknown_result_is_not_replayed(self):
        from backend.ai.providers import ProviderError, ProviderResult
        row = self.request()
        with patch('backend.ai.runtime.credential_for', return_value='fixture-only'), \
             patch('backend.ai.providers.complete', return_value=ProviderResult('invalid', 'stop',
                 {'known': True, 'total_tokens': 10}, 'fixture')) as paid:
            self.assertEqual(summaries.generate_summary(row.id)['status'], 'failed')
            self.assertEqual(paid.call_count, 2)
        row = self.request()
        with patch('backend.ai.runtime.credential_for', return_value='fixture-only'), \
             patch('backend.ai.providers.complete', side_effect=ProviderError('read_timeout', uncertain=True)) as paid:
            self.assertEqual(summaries.generate_summary(row.id)['status'], 'uncertain')
            self.assertEqual(summaries.request_summary(self.ann)['status'], 'uncertain')
            summaries.generate_summary(row.id)
            self.assertEqual(paid.call_count, 1)

    def test_summary_version_export_import_is_additive_and_stale_goes_to_history(self):
        from backend.services.data_transfer import export_data, read_backup, merge_data
        row = self.request(); self.run_row(row)
        with export_data() as stream:
            data = read_backup(stream)
        self.assertIn('summary_version', data['announcements'][0])
        AnnouncementSummary.query.delete(); db.session.commit()
        merge_data(data)
        self.assertTrue(summaries.current_summary(self.ann))
        self.assertEqual(AnnouncementSummary.query.count(), 1)
        merge_data(data)
        self.assertEqual(AnnouncementSummary.query.count(), 1)
        AnnouncementSummary.query.delete()
        self.ann.content_text += '新增适用条件。'; db.session.commit()
        merge_data(data)
        self.assertFalse(summaries.current_summary(self.ann))
        self.assertEqual(AnnouncementSummary.query.one().state, 'historical')
        self.assertFalse(summaries.summary_status(self.ann)['summary'])

    def test_current_list_projection_does_not_load_deferred_bodies(self):
        from sqlalchemy import event
        from sqlalchemy.orm import defer
        row = self.request(); self.run_row(row)
        db.session.expire_all()
        announcements = Announcement.query.options(defer(Announcement.content_text)).all()
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', capture)
        try:
            self.assertTrue(summaries.current_summaries(announcements)[self.ann_id])
        finally:
            event.remove(db.engine, 'before_cursor_execute', capture)
        self.assertEqual(len(statements), 1)

    def test_ai_settings_admin_permissions_and_passive_operations_never_pay(self):
        from backend.ai.models import AIExecution
        headers = {'X-CSRF-Token': 'token'}
        reader = self.client(self.reader)
        admin = self.client(self.admin)
        for path, method in [('/api/admin/ai', 'get'), ('/api/admin/ai/profiles', 'post'),
                ('/api/admin/ai/profiles/1/test', 'post'), ('/api/admin/ai/profiles/1', 'delete'),
                ('/api/admin/ai/profiles/1/key', 'post'), ('/api/admin/ai/profiles/1', 'put'),
                ('/api/admin/ai/bindings/summary', 'put'), ('/api/admin/ai/limits', 'put')]:
            self.assertEqual(getattr(reader, method)(path, headers=headers).status_code, 403)
        with patch('backend.ai.providers.complete', side_effect=AssertionError('paid operation')):
            self.assertEqual(admin.get('/api/admin/ai').status_code, 200)
            self.assertEqual(admin.put('/api/admin/ai/limits', json={'total': 0}, headers=headers).status_code, 200)
        self.assertEqual(AIExecution.query.count(), 0)

    def test_admin_can_explicitly_reveal_toggle_and_delete_saved_service(self):
        import os
        from backend.ai.models import AIProfile, AIExecution
        client = self.client(self.admin)
        headers = {'X-CSRF-Token': 'token'}
        with patch.dict(os.environ, {'FIELD_ENC_KEY': 'isolated-key-reveal-test'}), \
             patch('backend.ai.providers.complete') as paid:
            created = client.post('/api/admin/ai/profiles', headers=headers, json={
                'provider': 'deepseek', 'model': 'fixture-model', 'api_key': 'fixture-saved-key'})
            self.assertEqual(created.status_code, 200)
            profile = created.get_json()
            path = '/api/admin/ai/profiles/' + str(profile['id'])
            self.assertNotIn('fixture-saved-key', created.get_data(as_text=True))
            self.assertNotIn('fixture-saved-key', client.get('/api/admin/ai').get_data(as_text=True))
            self.assertNotIn('fixture-saved-key', db.session.get(AIProfile, profile['id']).encrypted_key)
            payload = {'expected_version': profile['version']}
            self.assertEqual(client.post(path + '/key', json=payload).status_code, 403)
            self.assertEqual(client.get(path + '/key').status_code, 405)
            stale = client.post(path + '/key', json={'expected_version': 999}, headers=headers)
            self.assertEqual(stale.get_json()['error_code'], 'configuration_changed')
            revealed = client.post(path + '/key', json=payload, headers=headers)
            self.assertEqual(revealed.status_code, 200)
            self.assertEqual(revealed.get_json(), {'api_key': 'fixture-saved-key'})
            self.assertEqual(revealed.headers['Cache-Control'], 'no-store')
            with patch.dict(os.environ, {'FIELD_ENC_KEY': 'different-test-master-key'}):
                unavailable = client.post(path + '/key', json=payload, headers=headers)
                self.assertEqual(unavailable.get_json()['error_code'], 'credential_unavailable')
            row = db.session.get(AIProfile, profile['id'])
            row.enabled, row.tested_version = True, row.version
            db.session.commit()
            self.assertEqual(client.put(path, json={'enabled': False}).status_code, 403)
            self.assertEqual(client.put(path, json={'enabled': False}, headers=headers).status_code, 200)
            disabled = client.get('/api/admin/ai').get_json()['profiles'][0]
            self.assertFalse(disabled['enabled'])
            self.assertTrue(disabled['has_key'])
            self.assertTrue(disabled['tested'])
            revealed = client.post(path + '/key', json={'expected_version': disabled['version']}, headers=headers)
            self.assertEqual(revealed.get_json(), {'api_key': 'fixture-saved-key'})
            enabled = client.put(path, json={'enabled': True, 'expected_version': disabled['version']}, headers=headers)
            self.assertTrue(enabled.get_json()['enabled'])
            self.assertEqual(client.delete(path, headers=headers).status_code, 200)
            self.assertEqual(client.get('/api/admin/ai').get_json()['profiles'], [])
            db.session.expire_all()
            self.assertEqual(db.session.get(AIProfile, profile['id']).encrypted_key, '')
            self.assertEqual(client.post(path + '/key', json=payload, headers=headers).get_json()['error_code'], 'not_found')
            self.assertEqual(client.put(path, json={'api_key': 'another-fixture-key'}, headers=headers).status_code, 400)
            paid.assert_not_called()
            self.assertEqual(AIExecution.query.count(), 0)

    def test_imported_source_rules_wait_for_review_and_keep_existing_rules(self):
        from backend.services.data_transfer import export_data, read_backup, merge_data
        from backend.database.source_governance_models import SourceProposal
        self.ann.department.list_selector = 'ul.current li'
        db.session.commit()
        with export_data() as stream:
            data = read_backup(stream)
        data['departments'][0]['list_selector'] = 'ul.imported li'
        data['departments'][0]['title_selector'] = 'a'
        data['departments'][0]['link_selector'] = 'a'
        result = merge_data(data)
        self.assertEqual(result['sources_pending_review'], 1)
        db.session.refresh(self.ann.department)
        self.assertEqual(self.ann.department.list_selector, 'ul.current li')
        data['departments'][0]['name'] = '导入的另一个栏目'
        merge_data(data)
        imported = Department.query.filter_by(name='导入的另一个栏目').one()
        self.assertEqual(imported.list_selector, '')
        self.assertEqual(SourceProposal.query.filter_by(department_id=imported.id).count(), 1)
        self.assertEqual(BackgroundTask.query.filter_by(kind='source_review').count(), 2)

    def test_import_failure_rolls_back_sources_proposals_and_queue_together(self):
        from backend.services.data_transfer import export_data, read_backup, merge_data
        from backend.database.source_governance_models import SourceProposal
        with export_data() as stream:
            data = read_backup(stream)
        data['departments'][0].update(name='不能部分提交的栏目', list_selector='ul.notices li', title_selector='a')
        original = tasks.enqueue
        def fail_after_queue(*args, **kwargs):
            self.assertFalse(kwargs['commit'])
            original(*args, **kwargs)
            raise RuntimeError('simulated final import failure')
        with patch('backend.services.tasks.enqueue', side_effect=fail_after_queue):
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                merge_data(data)
        self.assertIsNone(Department.query.filter_by(name='不能部分提交的栏目').first())
        self.assertEqual(SourceProposal.query.count(), 0)
        self.assertEqual(BackgroundTask.query.filter_by(kind='source_review').count(), 0)
        self.assertEqual(Announcement.query.count(), 1)

    def test_concurrency_wait_defers_without_consuming_failure_attempt(self):
        from backend.ai.configuration import AIConfigError
        row = self.request()
        with patch('backend.services.summaries._call', side_effect=AIConfigError('正在等待', 'concurrency_limit')):
            with self.assertRaises(tasks.TaskDeferred) as waiting:
                summaries.generate_summary(row.id)
        self.assertEqual(waiting.exception.phase, 'summary_resource')
        self.assertEqual(row.state, 'pending')
        self.assertEqual(db.session.get(BackgroundTask, row.task_id).attempts, 0)

    def test_failed_forced_refresh_keeps_valid_text_and_shows_failure(self):
        row = self.request(); self.run_row(row)
        prior = summaries.current_summary(self.ann)
        row = self.request(force=True)
        with patch('backend.services.summaries._call', side_effect=ValueError('测试失败')):
            summaries.generate_summary(row.id)
        status = summaries.summary_status(self.ann)
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['summary'], prior)
        self.assertEqual(status['error'], '测试失败')

    def test_import_school_identity_reuses_name_after_url_change_and_separates_campus(self):
        from backend.services.data_transfer import export_data, read_backup, merge_data
        with export_data() as stream:
            data = read_backup(stream)
        original_id = self.ann.school_id
        data['schools'][0]['url'] = 'https://new.example.edu.cn'
        self.assertEqual(merge_data(data)['schools_added'], 0)
        self.assertEqual(School.query.one().id, original_id)
        self.assertEqual(School.query.one().url, 'https://example.edu.cn')
        data['schools'][0]['name'] = '测试学校异地校区'
        data['schools'][0]['url'] = 'https://example.edu.cn'
        self.assertEqual(merge_data(data)['schools_added'], 1)
        self.assertEqual(School.query.count(), 2)
        self.assertNotEqual(School.query.filter_by(name='测试学校异地校区').one().id, original_id)

    def test_import_ambiguous_existing_school_name_rolls_back(self):
        from backend.services.data_transfer import export_data, read_backup, merge_data
        with export_data() as stream:
            data = read_backup(stream)
        db.session.add(School(name='测试学校', url='https://other.example.edu.cn'))
        db.session.commit()
        with self.assertRaisesRegex(ValueError, '身份存在重复'):
            merge_data(data)
        self.assertEqual(School.query.count(), 2)
        self.assertEqual(Announcement.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
