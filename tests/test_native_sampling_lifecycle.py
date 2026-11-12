"""Financial and task ownership boundaries, not website-effectiveness tests."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.checks import native_sampling_lifecycle as lifecycle


class FreshSamplingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sampling-lifecycle-')
        self.addCleanup(self.temp.cleanup)
        self.instance = Path(self.temp.name)
        self.output = self.instance / 'output'
        self.output.mkdir()
        self.path = self.instance / 'school_watcher.db'
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executescript('''
                CREATE TABLE app_config(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO app_config VALUES('ai_token_limit_total','200000'),('ai_token_limit_directory','200000');
                CREATE TABLE ai_budgets(key TEXT,used_tokens INTEGER,reserved_tokens INTEGER);
                INSERT INTO ai_budgets VALUES('total:test',189267,123);
                CREATE TABLE schools(id INTEGER,name TEXT,url TEXT);
                INSERT INTO schools VALUES(5,'fresh','https://fresh.edu/');
                CREATE TABLE background_tasks(id INTEGER,payload TEXT,state TEXT,generation INTEGER,
                    token TEXT,worker_id TEXT,lease_until TEXT,available_at TEXT,checkpoint TEXT);
            ''')
        self.scope = dict(school=dict(name='fresh',root_url='https://fresh.edu/'),existing_max_task_id=110,
            temporary_token_limits={k:dict(before='200000',during='400000') for k in lifecycle.LIMIT_KEYS})

    def values(self):
        with closing(sqlite3.connect(self.path)) as c:
            return dict(c.execute('SELECT * FROM app_config')), c.execute('SELECT * FROM ai_budgets').fetchall()

    def test_restore_changes_only_limits_and_preserves_paid_and_reserved_usage(self):
        before = self.values()
        lifecycle.apply_temporary_limits(self.instance,self.scope,self.output)
        self.assertEqual(set(self.values()[0].values()),{'400000'})
        lifecycle.restore_temporary_limits(self.instance,self.scope,self.output)
        self.assertEqual(self.values(),before)

    def test_durable_intent_recovers_an_interrupted_child(self):
        lifecycle.apply_temporary_limits(self.instance,self.scope,self.output)
        intent = json.loads((self.output/'temporary-limit-intent.json').read_text())
        self.assertTrue(intent['written_before_commit'])
        # No normal child completion is needed for the parent rollback.
        lifecycle.restore_temporary_limits(self.instance,self.scope,self.output)
        self.assertEqual(set(self.values()[0].values()),{'200000'})

    def test_external_edit_is_preserved_without_partially_restoring_other_keys(self):
        lifecycle.apply_temporary_limits(self.instance,self.scope,self.output)
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute("UPDATE app_config SET value='350000' WHERE key='ai_token_limit_total'")
        before = self.values()
        with self.assertRaisesRegex(ValueError,'external limit edit'):
            lifecycle.restore_temporary_limits(self.instance,self.scope,self.output)
        self.assertEqual(self.values(),before)

    def test_new_school_window_restores_limits_after_failure(self):
        before = self.values()
        with self.assertRaisesRegex(RuntimeError,'child failed'):
            with lifecycle.fresh_school_window(self.instance,self.scope,self.output):
                lifecycle.apply_temporary_limits(self.instance,self.scope,self.output)
                raise RuntimeError('child failed')
        self.assertEqual(self.values(),before)

    def test_parking_preserves_old_tasks_other_schools_and_external_ownership(self):
        fixtures=[(110,5,'pending',None),(111,5,'pending',None),(112,4,'pending',None),
                  (113,5,'running','declared-native-pilot'),(114,5,'running','another-worker')]
        with closing(sqlite3.connect(self.path)) as c,c:
            for identity,school,state,worker in fixtures:
                c.execute('INSERT INTO background_tasks VALUES(?,?,?,?,?,?,?,?,?)',
                    (identity,json.dumps(dict(school_id=school)),state,3,'token'if worker else None,
                     worker,'future'if worker else None,'original','keep-checkpoint'))
            before={r[0]:r for r in c.execute('SELECT * FROM background_tasks')}
        result=lifecycle.park_fresh_school_tasks(self.instance,self.scope,child_confirmed_stopped=True)
        self.assertEqual(result['parked_ids'],[111,113])
        self.assertEqual(result['fenced_ids'],[113])
        self.assertEqual(result['unresolved_owned_ids'],[114])
        with closing(sqlite3.connect(self.path)) as c:
            after={r[0]:r for r in c.execute('SELECT * FROM background_tasks')}
        for identity in (110,112,114):self.assertEqual(after[identity],before[identity])
        for identity in (111,113):
            self.assertEqual(after[identity][3],3)
            self.assertEqual(after[identity][-1],'keep-checkpoint')

    def test_a_timeout_observation_does_not_prove_the_owned_child_stopped(self):
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('INSERT INTO background_tasks VALUES(111,?,?,?,?,?,?,?,?)',
                (json.dumps(dict(school_id=5)),'running',3,'token','declared-native-pilot','future','original','keep'))
            before=c.execute('SELECT * FROM background_tasks').fetchone()
        result=lifecycle.park_fresh_school_tasks(self.instance,self.scope,child_confirmed_stopped=False)
        self.assertEqual(result['unresolved_owned_ids'],[111])
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT * FROM background_tasks').fetchone(),before)
