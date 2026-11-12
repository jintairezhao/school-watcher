"""Admission and bounded mutable-cache checks; no website or provider calls."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.checks import run_native_branch_pilot as pilot
from scripts.checks import native_sampling_admission as admission


class NativeSamplingAdmissionTests(unittest.TestCase):
    def setUp(self):
        pilot.BASE.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='sampling-admission-', dir=pilot.BASE)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'school_watcher.db'
        self.month = datetime.now(timezone.utc).strftime('%Y-%m')
        with sqlite3.connect(self.database) as c:
            c.executescript('''
                create table ai_profiles(id integer,provider text,model text,region text,version integer,
                    tested_version integer,enabled integer,deleted_at text,credential text);
                insert into ai_profiles values(1,'deepseek','deepseek-flash','default',1,1,1,null,'must-not-read');
                create table ai_bindings(purpose text,profile_id integer);
                insert into ai_bindings values('directory',1);
                create table app_config(id integer primary key,key text unique,value text);
                create table ai_budgets(key text primary key,used_tokens integer,reserved_tokens integer,
                    active_count integer,used_requests integer,reserved_requests integer);
                create table ai_school_rounds(school_id integer,budget_key text);
                insert into ai_school_rounds values(4,'school-discovery:4:77:1');
            ''')
            for key in ('total', 'directory', 'school_discovery'):
                c.execute('insert into app_config(key,value) values(?,?)', ('ai_token_limit_' + key, '200000'))
            c.execute('insert into app_config(key,value) values(?,?)', ('ai_request_limit_school_discovery', '82'))
            for key in ('total:' + self.month, 'directory:' + self.month):
                c.execute('insert into ai_budgets values(?,?,0,0,59,0)', (key, 187717))
            c.execute("insert into ai_budgets values('school-discovery:4:77:1',54178,0,0,17,0)")
        c.close()
        (self.root / 'control').mkdir()
        pilot.save(self.root / 'control/model-attempts.json', {'used': 102})
        self.request = self.root / 'known-request.json'
        pilot.save(self.request, dict(event='provider_request', payload=dict(body=dict(
            model='deepseek-flash', max_tokens=4096,
            messages=[dict(role='system', content='材料' * 4000),
                      dict(role='user', content='以下 JSON 为待分析资料：\n{"school_id":4}')]))))
        self.scope = dict(instance_path=self.root.relative_to(pilot.ROOT).as_posix(),
            continuation_school_id=4, native_school_request_limit=82,
            model=dict(maximum_new_dispatch_attempts=12, maximum_dispatch_attempts=114,
                       existing_dispatch_attempts=102, model='deepseek-flash'),
            model_admission_request_refs=[dict(path=self.request.relative_to(pilot.ROOT).as_posix(),
                                               sha256=pilot.sha(self.request))])
        self.scope_file = self.root / 'scope.json'
        pilot.save(self.scope_file, self.scope)

    def test_metadata_exposes_monthly_headroom_instead_of_only_binding_readiness(self):
        state = pilot.metadata(self.scope)
        self.assertTrue(state['directory_binding_ready'])
        admission = state.get('model_budget_admission', {})
        self.assertEqual(admission.get('status'), 'known_requests_blocked')
        self.assertFalse(admission['ready'])
        self.assertEqual(admission['lanes'][0]['remaining_tokens'], 12283)
        self.assertFalse(admission['planned_call_count_guaranteed'])

    def test_insufficient_headroom_blocks_before_app_start_or_output_creation(self):
        output = self.root / 'not-started'
        with patch.dict('os.environ'), patch('backend.create_app', side_effect=RuntimeError('app started too early')):
            with self.assertRaisesRegex(AssertionError, 'Native model budget admission'):
                pilot.native_child(self.scope_file, output, time.monotonic() + 10)
        self.assertFalse(output.exists())

    def test_a_fitting_known_request_does_not_promise_the_whole_call_allowance(self):
        with sqlite3.connect(self.database) as c:
            c.execute('update ai_budgets set used_tokens=0')
        c.close()
        before = pilot.sha(self.database)
        result = pilot.require_model_admission(self.scope)
        self.assertEqual(result['status'], 'known_requests_fit')
        self.assertTrue(result['ready'])
        self.assertFalse(result['planned_call_count_guaranteed'])
        self.assertEqual(pilot.sha(self.database), before)

    def test_unlimited_total_still_checks_the_directory_limit(self):
        with sqlite3.connect(self.database) as c:
            c.execute("update app_config set value='0' where key='ai_token_limit_total'")
        c.close()
        report = pilot.metadata(self.scope)['model_budget_admission']
        self.assertIsNone(report['lanes'][0]['remaining_tokens'])
        self.assertEqual(report['known_requests'][0]['blocked_by'], ['directory:' + self.month])

    def test_missing_or_changed_envelopes_never_report_ready(self):
        scope = dict(self.scope, model_admission_request_refs=[])
        self.assertEqual(pilot.metadata(scope)['model_budget_admission']['status'], 'unknown_request_envelopes')
        self.request.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'envelope changed'):
            pilot.require_model_admission(self.scope)

    def test_existing_reservations_are_reported_without_resetting_them(self):
        with sqlite3.connect(self.database) as c:
            c.execute('update ai_budgets set used_tokens=0,reserved_tokens=100,active_count=1')
        c.close()
        before = pilot.sha(self.database)
        self.assertEqual(pilot.metadata(self.scope)['model_budget_admission']['status'], 'unsettled_model_resources')
        self.assertEqual(pilot.sha(self.database), before)

    def test_zero_model_scope_does_not_need_a_paid_request_envelope(self):
        scope = dict(self.scope, model=dict(maximum_new_dispatch_attempts=0), model_admission_request_refs=[])
        self.assertEqual(pilot.require_model_admission(scope)['status'], 'no_new_models')

    def test_another_schools_envelope_cannot_establish_model_readiness(self):
        value = pilot.read(self.request)
        value['payload']['body']['messages'][1]['content'] = '{"school_id":3}'
        pilot.save(self.request, value)
        self.scope['model_admission_request_refs'][0]['sha256'] = pilot.sha(self.request)
        with self.assertRaisesRegex(ValueError, 'school differs'):
            pilot.require_model_admission(self.scope)

    def fresh_school_scope(self):
        scope = dict(self.scope, new_school_in_existing_instance=True,
                     school=dict(name='未运行学校', root_url='https://example.edu/'),
                     model_admission_request_refs=[])
        scope.pop('continuation_school_id')
        return scope

    def test_fresh_school_can_initialize_without_claiming_a_future_request_fits(self):
        scope = self.fresh_school_scope()
        before = pilot.sha(self.database)
        with patch.object(pilot, 'check_added_school') as guard:
            report = pilot.require_model_admission(scope)
        guard.assert_called_once_with(scope)
        self.assertTrue(report['bootstrap_ready'])
        self.assertFalse(report['ready'])
        self.assertFalse(report['planned_call_count_guaranteed'])
        self.assertTrue(report['actual_request_reservation_required'])
        self.assertEqual(report['known_requests'], [])
        self.assertEqual(pilot.sha(self.database), before)

    def test_fresh_school_bootstrap_still_requires_the_new_school_state_guard(self):
        with patch.object(pilot, 'check_added_school', side_effect=AssertionError('School inventory changed')):
            with self.assertRaisesRegex(AssertionError, 'School inventory changed'):
                pilot.require_model_admission(self.fresh_school_scope())

    def test_fresh_school_with_no_monthly_headroom_cannot_bootstrap(self):
        with sqlite3.connect(self.database) as c:
            c.execute('update ai_budgets set used_tokens=200000')
        c.close()
        before = pilot.sha(self.database)
        with patch.object(pilot, 'check_added_school'):
            with self.assertRaisesRegex(AssertionError, 'Native model budget admission'):
                pilot.require_model_admission(self.fresh_school_scope())
        self.assertEqual(pilot.sha(self.database), before)

    def test_an_existing_school_cannot_use_the_fresh_school_flag_to_skip_envelopes(self):
        scope = dict(self.scope, new_school_in_existing_instance=True, model_admission_request_refs=[])
        with patch.object(pilot, 'check_added_school') as guard:
            with self.assertRaisesRegex(AssertionError, 'Native model budget admission'):
                pilot.require_model_admission(scope)
        guard.assert_not_called()

    def test_fresh_school_needs_the_prospective_school_token_setting(self):
        with sqlite3.connect(self.database) as c:
            c.execute("delete from app_config where key='ai_token_limit_school_discovery'")
        c.close()
        with patch.object(pilot, 'check_added_school'):
            with self.assertRaisesRegex(AssertionError, 'Native model budget admission'):
                pilot.require_model_admission(self.fresh_school_scope())

    def test_fresh_school_does_not_reconcile_or_clear_an_active_reservation(self):
        with sqlite3.connect(self.database) as c:
            c.execute('update ai_budgets set reserved_tokens=100,active_count=1')
        c.close()
        before = pilot.sha(self.database)
        with patch.object(pilot, 'check_added_school'):
            with self.assertRaisesRegex(AssertionError, 'Native model budget admission'):
                pilot.require_model_admission(self.fresh_school_scope())
        self.assertEqual(pilot.sha(self.database), before)

    def test_fresh_school_requires_the_tested_model_binding(self):
        with sqlite3.connect(self.database) as c:
            c.execute('update ai_profiles set tested_version=0')
        c.close()
        with patch.object(pilot, 'check_added_school'):
            with self.assertRaisesRegex(AssertionError, 'binding'):
                pilot.require_model_admission(self.fresh_school_scope())


class NativeRouteCacheProtectionTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        self.c.executescript('''
            create table app_config(id integer primary key,key text unique,value text);
            create table background_tasks(id integer primary key,payload text,checkpoint text,state text);
        ''')
        self.now = 1700000000
        self.expired, self.new, self.live = 'a' * 64, 'b' * 64, 'c' * 64
        self.before = {self.expired: self.hint(self.now - 700), self.live: self.hint(self.now - 20)}
        self.c.execute('insert into app_config values(8,?,?)', (admission.CACHE_KEY, json.dumps(self.before)))
        self.c.execute("insert into app_config values(9,'source_configuration','must-stay')")
        self.c.execute('insert into background_tasks values(77,?,?,?)', (
            json.dumps(dict(school_id=4)), json.dumps(dict(http_failed_proxy_fingerprints={
                'jobs.example.edu.cn': self.new})), 'pending'))
        self.context = dict(initial_documents=0, initial_http_attempts=0, admitted_task_ids=[77],
                            existing_max_task_id=77, school_id=4, allowed_school_domain='example.edu.cn')
        self.tables = {}
        for table in ('app_config', 'background_tasks'):
            cursor = self.c.execute('select * from ' + table)
            names = [column[0] for column in cursor.description]
            self.tables[table] = dict(primary_key=['id'], rows_sha256={admission.canonical([r[0]]):
                admission.fingerprint(dict(zip(names, r))) for r in cursor})
        self.cert = admission.protect_route_cache(self.c, self.tables, self.context)
        iso = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        self.ledger = dict(documents=[dict(sequence=1, task_id=77, started_at=iso(self.now + 10),
            finished_at=iso(self.now + 20))], http_attempts=[dict(sequence=1, document=1,
            host='jobs.example.edu.cn', status=200)])

    def hint(self, verified):
        return dict(verified_at=verified, expires_at=verified + 600, basis=admission.CACHE_BASIS)

    def test_incomplete_document_does_not_authorize_a_new_route_hint(self):
        ledger=json.loads(json.dumps(self.ledger))
        ledger['documents'][0].pop('finished_at')
        after=dict(self.before,**{self.new:self.hint(self.now+15)})
        self.c.execute('UPDATE app_config SET value=? WHERE key=?',(json.dumps(after),admission.CACHE_KEY))
        report=admission.verify_row_protection(self.c,self.cert,ledger=ledger)
        self.assertTrue(report['errors'])

    def test_incomplete_document_does_not_discard_an_earlier_complete_success(self):
        ledger=json.loads(json.dumps(self.ledger))
        ledger['documents'].append(dict(sequence=2,task_id=77,
            started_at=datetime.fromtimestamp(self.now+25,timezone.utc).isoformat()))
        ledger['http_attempts'].append(dict(sequence=2,document=2,host='jobs.example.edu.cn',status=200))
        after=dict(self.before,**{self.new:self.hint(self.now+15)})
        self.c.execute('UPDATE app_config SET value=? WHERE key=?',(json.dumps(after),admission.CACHE_KEY))
        report=admission.verify_row_protection(self.c,self.cert,ledger=ledger)
        self.assertFalse(report['errors'])

    def update(self, value):
        self.c.execute('update app_config set value=? where id=8', (json.dumps(value),))

    def verified(self):
        return admission.verify_row_protection(self.c, self.cert, ledger=self.ledger)

    def valid_update(self):
        return {self.live: self.before[self.live], self.new: self.hint(self.now + 12)}

    def test_scoped_success_and_expired_cleanup_are_recorded_separately(self):
        self.update(self.valid_update())
        result = self.verified()
        self.assertEqual(result['errors'], [])
        self.assertEqual({x['change'] for x in result['runtime_cache_changes']},
                         {'expired_hint_removed', 'scoped_success_hint_recorded'})

    def test_source_configuration_and_old_task_changes_still_fail(self):
        self.update(self.valid_update())
        self.c.execute("update app_config set value='wrong' where id=9")
        self.c.execute("update background_tasks set state='running' where id=77")
        errors = self.verified()['errors']
        self.assertEqual({x['table'] for x in errors}, {'app_config', 'background_tasks'})

    def test_unobserved_or_external_host_hints_are_not_whitelisted(self):
        self.update(self.valid_update())
        self.ledger['http_attempts'][0]['host'] = 'outside.example.net'
        self.assertIn('hint_without_scoped_success', {x.get('reason') for x in self.verified()['errors']})

    def test_failure_response_does_not_authorize_a_direct_success_hint(self):
        self.update(self.valid_update())
        self.ledger['http_attempts'][0]['status'] = 403
        self.assertTrue(self.verified()['errors'])

    def test_live_hint_removal_and_clock_or_lifetime_tampering_fail(self):
        self.update({self.new: self.hint(self.now + 12)})
        self.assertIn('live_hint_removed', {x.get('reason') for x in self.verified()['errors']})
        changed = self.valid_update()
        changed[self.new]['expires_at'] += 1000
        self.update(changed)
        self.assertEqual(self.verified()['errors'][0]['reason'], 'invalid_route_cache')

    def test_legacy_certificate_does_not_retroactively_allow_the_old_failure(self):
        self.update(self.valid_update())
        result = admission.verify_row_protection(self.c, self.tables)
        self.assertEqual(result['errors'], [dict(table='app_config', identity='[8]')])
        self.assertTrue(result['legacy_certificate_strict'])

    def test_capacity_cleanup_requires_128_newer_surviving_hints(self):
        before = dict(id=8, key=admission.CACHE_KEY, value=json.dumps({self.live: self.hint(self.now - 20)}))
        values = {format(index, '064x'): self.hint(self.now + 12 + index / 1000) for index in range(128)}
        observed = {key: [(self.now + 10, self.now + 20)] for key in values}
        after = dict(id=8, key=admission.CACHE_KEY, value=json.dumps(values))
        errors, changes = admission.verify_route_cache(before, after, observed, self.now + 20)
        self.assertFalse(errors)
        self.assertIn('oldest_hint_evicted_at_runtime_capacity', {r['change'] for r in changes})
        values.pop(next(iter(values)))
        after['value'] = json.dumps(values)
        self.assertIn('live_hint_removed', admission.verify_route_cache(before, after, observed, self.now + 20)[0])

    def test_declaring_cache_mutability_does_not_mutate_the_strict_certificate(self):
        self.assertIn('[8]', self.tables['app_config']['rows_sha256'])
        self.assertNotIn('[8]', self.cert['tables']['app_config']['rows_sha256'])

    def test_cache_contract_matches_the_actual_runtime_producer(self):
        from backend.services import http_route_memory
        self.assertEqual((admission.CACHE_TTL, admission.MAX_CACHE_ENTRIES, admission.CACHE_KEY),
                         (http_route_memory.TTL_SECONDS, http_route_memory.MAX_ORIGINS, http_route_memory.KEY))

    def test_actual_wrapper_uses_the_declared_cache_policy_and_retains_the_strict_result(self):
        self.update(self.valid_update())
        self.c.commit()
        pilot.BASE.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='cache-protection-', dir=pilot.BASE) as folder:
            instance = Path(folder)
            with sqlite3.connect(instance / 'school_watcher.db') as disk:
                self.c.backup(disk)
            disk.close()
            (instance / 'control').mkdir()
            pilot.save(instance / 'control/pilot-web-ledger.json', self.ledger)
            from types import SimpleNamespace
            wrapper = SimpleNamespace(last_protection_report=None)
            def verify_old_rows(target, certificate):
                with closing(sqlite3.connect((target/'school_watcher.db').as_uri()+'?mode=ro',uri=True)) as connection:
                    connection.row_factory=sqlite3.Row
                    ledger=pilot.read(target/'control/pilot-web-ledger.json') if certificate.get('schema_version')==2 else None
                    wrapper.last_protection_report=admission.verify_row_protection(connection,certificate,ledger=ledger)
                return wrapper.last_protection_report['errors']
            wrapper.verify_old_rows=verify_old_rows
            self.assertEqual(wrapper.verify_old_rows(instance, self.tables),
                             [dict(table='app_config', identity='[8]')])
            self.assertEqual(wrapper.verify_old_rows(instance, self.cert), [])
            self.assertEqual(len(wrapper.last_protection_report['runtime_cache_changes']), 2)


if __name__ == '__main__':
    unittest.main()
