"""A task that keeps coming back must not hold the queue shut, and the position a
waiting school is shown must be the position the worker acts on.

The observed failure: 91 columns whose site was not answering were re-claimed 889
times between them, each returning to its old place at the head of the queue every
few seconds, while a school that had never been examined at all sat at position
158 of 161 -- and its screen said "已加入发现队列 / 进行中".
"""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

import test_shared_summaries as fixture_module
from backend import create_app
from backend.database.db import db
from backend.database.models import BackgroundTask, School, WorkerHeartbeat
from backend.services import tasks
from backend.services.onboarding_progress import status


class QueueFairnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watcher-queue-')
        root = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'queue-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(root / 'main.db'),
            'SOURCE_CATALOG_PATH': str(root / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(root / 'discovery.db'),
            'BACKUP_DIR': str(root / 'backups'), 'BACKUP_COPY_DIR': ''})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.school = School(name='例校', url='https://example.edu.cn', enabled=True, subscriber_count=1)
        db.session.add(self.school); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def backdate(self, rows, minutes):
        """Age rows the way a long-lived queue ages them."""
        when = datetime.utcnow() - timedelta(minutes=minutes)
        for row in rows if isinstance(rows, list) else [rows]:
            row.queued_at = when
        db.session.commit()

    def handed_off(self, count, turns):
        """Collection tasks that keep returning: a handoff retains queued_at and
        makes the row due again a few seconds later, so they are always claimable
        and never finish."""
        rows = [tasks.enqueue('collect', index + 1, {'school_id': self.school.id, 'department_id': index + 1})
                for index in range(count)]
        for row in rows:
            row.claim_count = turns
        db.session.commit()
        self.backdate(rows, 8)
        return rows

    def test_a_column_stuck_retrying_does_not_hold_the_queue_shut(self):
        self.handed_off(40, turns=60)
        waiting = tasks.enqueue('discover', self.school.id, {'school_id': self.school.id})
        self.backdate(waiting, 8)
        # Same age band, so only "has this ever run" can decide -- and the school
        # has never run while each column has had 60 turns.
        self.assertEqual(tasks.claim()['id'], waiting.id)

    def test_many_material_fragments_rotate_with_untouched_directory_tasks(self):
        older = [tasks.enqueue('source_review', i, {'proposal_id': i}) for i in range(10, 30)]
        self.backdate(older, 8)
        ready = tasks.enqueue('source_review', 1, {'proposal_id': 1})
        ready.phase = 'source_material'; ready.claim_count = 1
        db.session.commit()
        self.backdate(ready, 8)
        self.assertEqual(tasks.queue_ahead(ready)[0], len(older))
        self.assertEqual(tasks.claim()['id'], older[0].id)

    def test_first_collection_is_not_blocked_by_an_aged_discovery_backlog(self):
        older = [tasks.enqueue('source_review', i, {'proposal_id': i}) for i in range(10, 30)]
        self.backdate(older, 8)
        ready = tasks.enqueue('collect', 1, {'school_id': self.school.id, 'department_id': 1})
        ready.phase = 'onboarding_collection'
        db.session.commit()
        self.assertEqual(tasks.queue_ahead(ready)[0], 0)
        self.assertEqual(tasks.claim()['id'], ready.id)

    def test_a_paused_task_keeps_its_place_in_line(self):
        """继续 carries on with the interrupted work instead of waiting behind
        every task that has never run. The pause path says so explicitly."""
        paused = tasks.enqueue('collect', 1, {'school_id': self.school.id, 'department_id': 1})
        tasks.enqueue('collect', 2, {'school_id': self.school.id, 'department_id': 2})
        self.backdate(paused, 2)
        handle = tasks.claim()
        self.assertEqual(handle['id'], paused.id)
        self.assertEqual(db.session.get(BackgroundTask, paused.id).claim_count, 1)
        tasks.handoff(handle, tasks.TaskDeferred(capability='http', phase='fetch',
                                                reason='更改文件位置，抓取已暂停',
                                                error_code='desktop_paused', keep_place=True))
        self.assertEqual(db.session.get(BackgroundTask, paused.id).claim_count, 0)
        self.assertEqual(tasks.claim()['id'], paused.id)

    def test_first_in_first_out_still_decides_between_equal_turns(self):
        newer = tasks.enqueue('collect', 1, {'school_id': self.school.id, 'department_id': 1})
        older = tasks.enqueue('collect', 2, {'school_id': self.school.id, 'department_id': 2})
        self.backdate(older, 1)
        self.assertEqual(tasks.claim()['id'], older.id)
        self.assertEqual(tasks.claim()['id'], newer.id)

    def test_the_position_shown_is_the_task_the_worker_takes(self):
        """Every claim must take the task whose reported position is zero -- in a
        queue where some rows have already had many turns and some have had none."""
        re_claimed = self.handed_off(2, turns=20)
        rows = [tasks.enqueue('collect', index + 10, {'school_id': self.school.id, 'department_id': index + 10})
                for index in range(4)]
        rows.append(tasks.enqueue('discover', self.school.id, {'school_id': self.school.id}))
        self.backdate(rows, 8)
        rows = re_claimed + rows
        claimed = []
        for _ in range(len(rows)):
            pending = BackgroundTask.query.filter_by(state='pending').all()
            next_up = [row.id for row in pending if tasks.queue_ahead(row)[0] == 0]
            self.assertEqual(len(next_up), 1, 'exactly one task is next in the worker\'s order')
            handle = tasks.claim()
            self.assertEqual(handle['id'], next_up[0], 'the worker took a task that was not next')
            claimed.append(handle['id'])
        self.assertEqual(len(set(claimed)), len(rows), 'every task gets its turn')
        self.assertIsNone(tasks.claim())

    def test_a_position_is_counted_across_lanes_the_worker_can_claim(self):
        """A school waits behind work of other kinds, so counting only its own
        lane would report an empty queue for a task that is not next: here the
        three columns are ahead of the school, and none of them is a directory
        task."""
        db.session.add(WorkerHeartbeat(worker_id='w1', roles=['http', 'browser', 'directory'],
                                       heartbeat_at=datetime.utcnow()))
        db.session.commit()
        self.handed_off(3, turns=5)
        waiting = tasks.enqueue('discover', self.school.id, {'school_id': self.school.id})
        self.assertEqual(tasks.queue_ahead(waiting)[0], 3)
        self.assertEqual(tasks.queue_ahead(waiting, capability='directory')[0], 0)


class QueuedStatusTests(unittest.TestCase):
    """The discovery screen must distinguish waiting from working."""

    def setUp(self):
        self.fixture = fixture_module.SharedSummaryTests(); self.fixture.setUp()
        self.school_id = self.fixture.ann.school_id
        self.task = tasks.enqueue('discover', self.school_id, {'school_id': self.school_id})
        db.session.add(WorkerHeartbeat(worker_id='w1', roles=['http', 'browser', 'directory'],
                                       heartbeat_at=datetime.utcnow()))
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def backdate(self, rows, minutes=8):
        when = datetime.utcnow() - timedelta(minutes=minutes)
        for row in rows if isinstance(rows, list) else [rows]:
            row.queued_at = when
        db.session.commit()

    def test_waiting_is_reported_with_its_position_and_the_blocking_site_reads(self):
        self.fixture.app.config['WORKER_CONCURRENCY'] = 1
        blocked = tasks.enqueue('collect', 99, {'school_id': self.school_id, 'department_id': 99})
        blocked.attempts, blocked.phase = 1, 'retry'
        blocked.error = '连接或读取官网超时，请稍后重试'
        db.session.commit()
        # The column was queued first, the school ten minutes later: the same
        # order the copy's database was in.
        self.backdate(blocked, 18)
        self.backdate(self.task, 8)
        result = status(db.session.get(School, self.school_id))
        self.assertEqual(result['state'], 'queued')
        self.assertIn('正在排队：前面还有 1 个任务', result['message'])
        self.assertFalse(result['busy'])
        self.assertNotIn('官网访问失败', result['message'])
        # A queued school is still a live task: keep polling and keep the pause button.
        self.assertTrue(result['active'])
        self.assertTrue(result['can_pause'])
        self.assertFalse(result['can_retry'])

    def test_a_silent_worker_is_not_reported_as_merely_queued(self):
        """No worker means nothing will move, whatever the queue looks like."""
        db.session.query(WorkerHeartbeat).delete(); db.session.commit()
        self.backdate(self.task)
        result = status(db.session.get(School, self.school_id))
        self.assertEqual(result['state'], 'unavailable')
        self.assertIn('后台服务未响应', result['message'])

    def test_reserved_directory_slot_does_not_report_unrelated_http_backlog(self):
        self.fixture.app.config['WORKER_CONCURRENCY'] = 2
        blocked = tasks.enqueue('collect', 99, {'school_id': self.school_id, 'department_id': 99})
        self.backdate(blocked, 18)
        result = status(db.session.get(School, self.school_id))
        self.assertNotIn('前面还有', result['message'])


if __name__ == '__main__':
    unittest.main()
