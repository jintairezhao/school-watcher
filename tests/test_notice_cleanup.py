"""Destructive storage controls use only disposable, explicitly configured databases."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy import event, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.database.db import db
from backend.database.models import (Announcement, AnnouncementMerge, AnnouncementResource,
    AnnouncementSource, BackgroundTask, Department, RuntimeLease, School, ScrapeLog,
    Subscription, User, UserAnnouncementState, UserRead, VerificationSession, VerificationWaiter)
from backend.ai.models import AIExecution
from backend.ai.summary_models import AnnouncementSummary
from backend.database.student_information_models import StudentAssessment
from backend.services.notice_cleanup import NoticeCleanupError, delete_preview, preview


class NoticeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='notice-cleanup-test-only',
            SQLALCHEMY_DATABASE_URI='sqlite:///' + str(self.root / 'main.db'),
            SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': 1}})
        db.init_app(self.app)
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.actor = User(username='admin', password_hash='unused', role='admin')
        self.other = User(username='reader', password_hash='unused')
        self.school = School(name='School one', url='https://one.example.edu')
        self.school2 = School(name='School two', url='https://two.example.edu')
        db.session.add_all([self.actor, self.other, self.school, self.school2]); db.session.flush()
        self.dept = Department(school_id=self.school.id, name='Office')
        self.dept2 = Department(school_id=self.school2.id, name='Office two')
        db.session.add_all([self.dept, self.dept2]); db.session.flush()
        db.session.add(Subscription(user_id=self.other.id, school_id=self.school.id))
        db.session.commit()
        self.actor_id = self.actor.id

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def article(self, title='old', *, published_at=datetime(2020, 1, 1), other=False):
        dept = self.dept2 if other else self.dept
        row = Announcement(school_id=dept.school_id, department_id=dept.id, title=title,
            url='https://one.example.edu/' + title, published_at=published_at,
            created_at=datetime(2020, 1, 2), content_text='Body', content_html='<p>Body</p>')
        db.session.add(row); db.session.commit()
        return row

    def plan(self, **values):
        return preview({'school_id': None, 'before': '2021-01-01', **values}, self.actor_id)

    def remove(self, plan):
        return delete_preview(plan['preview_token'], self.actor_id)

    def error(self, function, status=400):
        with self.assertRaises(NoticeCleanupError) as result:
            function()
        self.assertEqual(result.exception.status_code, status)

    def task(self, article, state='done', payload=None, **values):
        job = BackgroundTask(identity='content:' + str(article.id), kind='content', state=state,
            payload=payload if payload is not None else {'announcement_id': article.id}, **values)
        db.session.add(job); db.session.commit(); return job

    def summary(self, article, state='succeeded', task_id=None):
        row = AnnouncementSummary(announcement_id=article.id, identity='summary-' + str(article.id),
            revision=1, input_hash='hash', title_snapshot=article.title, body_snapshot='Body',
            state=state, task_id=task_id)
        db.session.add(row); db.session.commit(); return row

    def test_preview_is_read_only_and_strictly_before_calendar_day(self):
        old = self.article('old', published_at=datetime(2020, 12, 31, 23, 59, 59))
        self.article('same-day', published_at=datetime(2021, 1, 1))
        self.article('unknown', published_at=None)
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement.strip().lower())
        event.listen(db.engine, 'before_cursor_execute', record)
        try:
            result = self.plan()
        finally:
            event.remove(db.engine, 'before_cursor_execute', record)
        self.assertEqual((result['matched_count'], result['deletable_count'], result['unknown_date_count']), (1, 1, 1))
        self.assertEqual(result['sample'][0]['id'], old.id)
        self.assertFalse(any(s.startswith(('insert', 'update', 'delete', 'create', 'vacuum')) for s in statements))
        self.assertEqual(Announcement.query.count(), 3)

    def test_filter_validation(self):
        for filters in (None, [], {}, {'before': '2021-1-01'}, {'before': '2021-02-29'},
                {'before': '2021-01-01T00:00:00'}, {'before': '2021-01-01', 'school_id': True},
                {'before': '2021-01-01', 'school_id': 0}, {'before': '2021-01-01', 'school_id': '1'},
                {'before': '2021-01-01', 'unknown': 1}):
            with self.subTest(filters=filters):
                self.error(lambda: preview(filters, self.actor_id))

    def test_school_scope_and_unknown_dates_are_preserved(self):
        old = self.article(); other = self.article('other', other=True)
        old_id = old.id
        unknown = self.article('unknown', published_at=None)
        result = self.remove(self.plan(school_id=self.school.id))
        self.assertEqual(result['deleted_count'], 1)
        self.assertEqual({a.id for a in Announcement.query.all()}, {other.id, unknown.id})
        self.assertIsNone(db.session.get(Announcement, old_id))

    def test_any_users_favorite_and_recent_trash_protected(self):
        starred = self.article('starred'); trash = self.article('trash'); expired = self.article('expired')
        db.session.add_all([UserAnnouncementState(user_id=self.other.id, announcement_id=starred.id, starred=True),
            UserAnnouncementState(user_id=self.actor_id, announcement_id=trash.id, trashed_at=datetime.utcnow() - timedelta(days=5)),
            UserAnnouncementState(user_id=self.other.id, announcement_id=expired.id, trashed_at=datetime.utcnow() - timedelta(days=31))])
        db.session.commit(); plan = self.plan()
        self.assertEqual((plan['matched_count'], plan['protected_count'], plan['deletable_count']), (3, 2, 1))
        self.assertEqual(self.remove(plan)['deleted_count'], 1)
        self.assertEqual(Announcement.query.count(), 2)

    def test_tampered_expired_and_other_actor_tokens(self):
        self.article(); token = self.plan()['preview_token']
        self.error(lambda: delete_preview(token + 'tamper', self.actor_id))
        self.error(lambda: delete_preview(token, self.other.id))
        with patch('itsdangerous.timed.time.time', return_value=4102444800):
            self.error(lambda: delete_preview(token, self.actor_id), 409)
        self.assertEqual(Announcement.query.count(), 1)

    def test_new_favorite_after_preview_invalidates_atomically(self):
        first = self.article('one'); self.article('two'); plan = self.plan()
        db.session.add(UserAnnouncementState(user_id=self.other.id, announcement_id=first.id, starred=True))
        db.session.commit(); self.error(lambda: self.remove(plan), 409)
        self.assertEqual(Announcement.query.count(), 2)

    def test_identity_and_date_changes_invalidate_preview(self):
        row = self.article(); plan = self.plan(); row.url = 'https://one.example.edu/reused'
        db.session.commit(); self.error(lambda: self.remove(plan), 409)
        plan = self.plan(); row.published_at = datetime(2019, 1, 1)
        db.session.commit(); self.error(lambda: self.remove(plan), 409)

    def test_deleted_id_reused_by_another_notice_invalidates_preview(self):
        row = self.article(); ident = row.id; plan = self.plan()
        db.session.delete(row); db.session.commit()
        replacement = Announcement(id=ident, school_id=self.school.id, department_id=self.dept.id,
            title='different notice', url='https://one.example.edu/replacement', published_at=datetime(2020, 1, 1))
        db.session.add(replacement); db.session.commit()
        self.error(lambda: self.remove(plan), 409)
        self.assertEqual(Announcement.query.one().title, 'different notice')

    def test_new_old_notification_is_outside_preview_high_water_mark(self):
        original = self.article('one'); plan = self.plan(); fresh = self.article('new')
        self.assertEqual(self.remove(plan)['deleted_count'], 1)
        self.assertEqual([a.id for a in Announcement.query.all()], [fresh.id])
        self.error(lambda: self.remove(plan), 409)

    def test_pending_running_waiting_tasks_protect_notifications(self):
        for index, state in enumerate(('pending', 'running', 'waiting')):
            self.task(self.article('job-' + str(index)), state=state)
        batch_article = self.article('batch')
        self.task(batch_article, state='pending', payload={'announcement_ids': [batch_article.id]})
        analysis = self.article('analysis')
        self.task(analysis, state='waiting', payload={'subject_kind': 'article', 'subject_id': analysis.id})
        self.assertEqual(self.plan()['protected_count'], 5)
        self.assertEqual(self.remove(self.plan())['deleted_count'], 0)

    def test_summary_resource_and_assessment_states_are_protected(self):
        summary = self.article('summary'); self.summary(summary, state='waiting_content')
        resource = self.article('resource')
        db.session.add(AnnouncementResource(announcement_id=resource.id, url_key='key', url=resource.url, state='pending'))
        assessment = self.article('assessment')
        db.session.add(StudentAssessment(subject_key='article:' + str(assessment.id),
            announcement_id=assessment.id, school_id=assessment.school_id, input_hash='hash', state='running'))
        db.session.commit(); self.assertEqual(self.plan()['protected_count'], 3)

    def test_summary_task_references_protect_even_if_summary_row_finished(self):
        row = self.article(); summary = self.summary(row)
        self.task(row, state='running', payload={'summary_id': summary.id})
        self.assertEqual(self.plan()['protected_count'], 1)

    def test_summary_task_foreign_key_protects_without_payload_reference(self):
        row = self.article(); task = self.task(row, state='running', payload={})
        self.summary(row, task_id=task.id)
        self.assertEqual(self.plan()['protected_count'], 1)

    def test_finished_summary_and_batch_json_are_not_left_orphaned(self):
        row = self.article(); summary = self.summary(row)
        self.task(row, payload={'summary_id': summary.id})
        second = BackgroundTask(identity='batch', kind='summarize', state='done', payload={'ids': [row.id]},
            result={'results': [{'announcement_id': row.id}]})
        db.session.add(second); db.session.commit()
        self.remove(self.plan())
        self.assertEqual(BackgroundTask.query.count(), 0)

    def test_merge_alias_chains_are_removed_transitively(self):
        row = self.article()
        db.session.add_all([AnnouncementMerge(old_id=999, survivor_id=row.id),
            AnnouncementMerge(old_id=1000, survivor_id=999)])
        db.session.commit(); self.remove(self.plan())
        self.assertEqual(AnnouncementMerge.query.count(), 0)

    def test_source_ids_are_not_mistaken_for_article_ids(self):
        row = self.article()
        self.task(row, state='running', payload={'subject_kind': 'source', 'subject_id': row.id, 'ids': [row.id]})
        self.assertEqual(self.plan()['protected_count'], 0)

    def test_sqlite_writer_cannot_star_halfway_through_cleanup(self):
        import sqlite3
        from contextlib import closing
        from backend.services import notice_cleanup
        row = self.article(); row_id = row.id; user_id = self.other.id; plan = self.plan()
        original = notice_cleanup._remove_related
        def attempt(connection, ids, summaries):
            with closing(sqlite3.connect(str(self.root / 'main.db'), timeout=0)) as writer:
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute('INSERT INTO user_announcement_states (user_id, announcement_id, starred, archived) VALUES (?, ?, 1, 0)',
                        (user_id, row_id))
            return original(connection, ids, summaries)
        with patch.object(notice_cleanup, '_remove_related', side_effect=attempt):
            self.assertEqual(self.remove(plan)['deleted_count'], 1)
        self.assertEqual(UserAnnouncementState.query.count(), 0)

    def test_new_active_task_after_preview_invalidates(self):
        row = self.article(); plan = self.plan(); self.task(row, state='pending')
        self.error(lambda: self.remove(plan), 409)
        self.assertEqual(Announcement.query.count(), 1)

    def test_related_records_removed_but_structure_subscriptions_and_usage_remain(self):
        row = self.article(); retained = self.article('retained', published_at=datetime(2022, 1, 1))
        job = self.task(row, token='finished-token'); job_id = job.id
        summary = self.summary(row, task_id=job_id)
        db.session.add_all([AnnouncementResource(announcement_id=row.id, url_key='resource', url=row.url, state='ready', payload=b'data'),
            AnnouncementSource(announcement_id=row.id, department_id=self.dept.id),
            UserRead(user_id=self.other.id, announcement_id=row.id),
            StudentAssessment(subject_key='article:' + str(row.id), announcement_id=row.id,
                school_id=row.school_id, input_hash='hash', state='ready'),
            AnnouncementMerge(old_id=999, survivor_id=row.id),
            RuntimeLease(key='finished', token='finished-token', owner_id='old', expires_at=datetime.utcnow()),
            ScrapeLog(school_id=row.school_id, task_id=job_id, status='success'),
            VerificationSession(id='session', source_id='source', origin='https://one.example.edu',
                url=row.url, status='completed', task_id=job_id),
            AIExecution(execution_id='historical', purpose='summary', config_version='1', provider='test',
                model='test', skill_id='test', skill_version='1', skill_digest='digest', input_digest='digest',
                mode='json', usage={'total_tokens': 71})])
        db.session.flush(); db.session.add(VerificationWaiter(task_id=job_id, session_id='session')); db.session.commit()
        result = self.remove(self.plan())
        self.assertEqual(result['deleted_count'], 1)
        for model in (AnnouncementResource, AnnouncementSource, UserRead, AnnouncementSummary,
                StudentAssessment, AnnouncementMerge, BackgroundTask, VerificationWaiter, RuntimeLease):
            self.assertEqual(db.session.query(model).count(), 0, model.__name__)
        self.assertEqual(db.session.get(VerificationSession, 'session').task_id, None)
        self.assertEqual(ScrapeLog.query.first().task_id, None)
        self.assertEqual(Subscription.query.count(), 1); self.assertEqual(Department.query.count(), 2)
        self.assertEqual(School.query.count(), 2)
        self.assertEqual(AIExecution.query.one().usage, {'total_tokens': 71})
        self.assertEqual([a.id for a in Announcement.query.all()], [retained.id])
        self.assertEqual(db.session.execute(text('PRAGMA foreign_key_check')).all(), [])

    def test_partial_delete_failure_rolls_back_every_table(self):
        row = self.article(); self.task(row); plan = self.plan()
        db.session.execute(text("CREATE TRIGGER deny_cleanup BEFORE DELETE ON announcements BEGIN SELECT RAISE(ABORT, 'test failure'); END"))
        db.session.commit()
        with self.assertLogs(self.app.logger, level='ERROR'):
            self.error(lambda: self.remove(plan), 409)
        self.assertEqual(Announcement.query.count(), 1)
        self.assertEqual(BackgroundTask.query.count(), 1)

    def test_unrelated_sqlite_writer_returns_quickly_without_mutation(self):
        import sqlite3
        from contextlib import closing
        from time import monotonic
        self.article(); plan = self.plan()
        with closing(sqlite3.connect(str(self.root / 'main.db'), timeout=0)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            started = monotonic()
            with self.assertLogs(self.app.logger, level='ERROR'):
                self.error(lambda: self.remove(plan), 409)
            elapsed = monotonic() - started
            writer.rollback()
        self.assertLess(elapsed, 2)
        self.assertEqual(Announcement.query.count(), 1)

    def test_maintenance_lock_prevents_concurrent_cleanup(self):
        from backend.services.database_space import storage_maintenance_lock
        self.article(); plan = self.plan()
        with storage_maintenance_lock():
            self.error(lambda: self.remove(plan), 409)
        self.assertEqual(Announcement.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
