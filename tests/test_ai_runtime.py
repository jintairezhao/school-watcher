"""No live providers: verify billing, isolation, contracts and single dispatch."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from flask import Flask

from backend.database.db import db
from backend.database import models
from backend.ai.models import AIProfile, AIBinding, AIExecution, AIBudget
from backend.ai.configuration import (save_profile, bind_profile, get_model_binding, public_settings,
                                     save_limits, delete_profile, credential_for, AIConfigError)
from backend.ai.providers import ProviderResult, ProviderError, complete
from backend.ai.runtime import run_skill, recover_uncertain_executions, reconcile_usage, test_connection
from backend.ai.skill_loader import load_skill, validate_input, validate_output, SkillValidationError
from backend.core.secrets import encrypt_field, decrypt_field, EncryptionUnavailable

EVIDENCE = {'title': '报名通知', 'paragraphs': [{'id':'p1','text':'面向本科生，报名截止 9月30日。'}], 'attachments_unread': True}
OUTPUT = {'summary': '本科生报名截止9月30日。', 'facts': [
    {'kind':'deadline','text':'报名截止9月30日','evidence_ids':['p1']}],
    'coverage': {'paragraph_ids':['p1'], 'attachments_included':False}}


class AIRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='watcher-ai-')
        self.env = patch.dict(os.environ, {'FIELD_ENC_KEY':'isolated-test-encryption-key', 'SECRET_KEY':'isolated-test'})
        self.env.start()
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///' + str(Path(self.tmp.name)/'test.db'),
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        row = save_profile({'name':'test','provider':'deepseek','model':'deepseek-chat','api_key':'never-a-real-key'})
        profile = db.session.get(AIProfile,row['id'])
        profile.enabled, profile.tested_version = True, profile.version
        db.session.commit()
        self.binding = bind_profile('summary', row['id'])
        bind_profile('directory', row['id'])

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.env.stop(); self.tmp.cleanup()

    def invoke(self, execution='one', **kwargs):
        return run_skill('summarize-university-notice','summary',EVIDENCE,'summary',execution,
                         binding=self.binding, **kwargs)

    def response(self, output=OUTPUT, usage=None):
        return ProviderResult(json.dumps(output,ensure_ascii=False),'stop',usage or
                              {'known':True,'input_tokens':50,'output_tokens':20,'total_tokens':70}, 'request-1')

    def test_encryption_fails_closed_but_legacy_read_is_supported(self):
        self.assertNotIn('never-a-real-key', json.dumps(public_settings()))
        ciphertext = encrypt_field('test-credential')
        self.assertEqual(decrypt_field(ciphertext),'test-credential')
        with patch.dict(os.environ, {'FIELD_ENC_KEY':''}):
            with self.assertRaises(EncryptionUnavailable): encrypt_field('secret')
            self.assertEqual(decrypt_field(ciphertext),'')
            self.assertEqual(decrypt_field('legacy-key'),'legacy-key')

    def test_single_execution_reuses_validated_result_and_records_actual_usage(self):
        def respond(*args, **kwargs):
            self.assertFalse(db.session().in_transaction(), 'network must not hold DB transaction')
            return self.response()
        with patch('backend.ai.providers.complete',side_effect=respond) as transport:
            first, second = self.invoke(), self.invoke()
        self.assertEqual(first['status'],'succeeded'); self.assertEqual(first,second)
        self.assertEqual(transport.call_count,1)
        self.assertEqual(AIExecution.query.count(),1)
        self.assertTrue(all(b.used_tokens==70 and b.reserved_tokens==0 and b.active_count==0 for b in AIBudget.query.all()))

    def test_student_value_uses_directory_budget_and_reuses_evidence(self):
        evidence = {'school_id': 1, 'candidates': [{'candidate_id': 'one', 'name': '往年通知',
            'url': 'https://example.edu.cn/old/', 'kind': 'notice_source', 'path': '财务处'}],
            'evidence': [{'candidate_id': 'one', 'evidence_id': 'p0', 'text': '本科生申请资助材料'}]}
        output = {'results': [{'candidate_id': 'one', 'role': 'publishing', 'value': 'relevant',
            'historical': 'high', 'reason': '准备材料', 'facts': [{'kind': 'history', 'text': '资助材料',
            'quote': '本科生申请资助材料', 'evidence_id': 'p0'}]}]}
        with patch('backend.ai.providers.complete', return_value=self.response(output)) as provider:
            first = run_skill('student-information', 'assess', evidence, 'directory', 'value-test')
            second = run_skill('student-information', 'assess', evidence, 'directory', 'value-test')
        self.assertEqual(first['status'], 'succeeded')
        self.assertEqual(first, second)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(AIExecution.query.one().purpose, 'directory')

    def test_school_discovery_budget_is_shared_by_children_and_continuations(self):
        from backend.ai.runtime import _reserve, _settle, AIBudgetError
        from backend.services import tasks
        save_limits({'school_discovery': 10})
        tasks.enqueue('discover', 3, {'school_id': 3})
        parent = tasks.claim(capabilities=['directory'])
        budget_key = f"school-discovery:3:{parent['id']}:{parent['generation']}"
        def reserve(name, amount):
            return _reserve(name, 'directory', self.binding, 'university-source-onboarding', '1',
                            'digest', 'material', 'column', amount)
        with tasks.execution_scope(parent):
            row, claimed = reserve('parent-reservation', 6)
        self.assertTrue(claimed)
        self.assertIn(budget_key, row.budget_keys)
        _settle(row.execution_id, status='succeeded', usage={'known': True, 'total_tokens': 4})
        tasks.enqueue('onboard', 'budget-child', {'school_id': 3, 'url': 'https://example.edu.cn/list/',
            'parent_task_id': parent['id'], 'parent_generation': parent['generation']})
        child = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(child):
            with self.assertRaises(AIBudgetError) as error:
                reserve('blocked-child', 7)
            self.assertEqual(error.exception.code, 'budget_exhausted')
            row, _ = reserve('fitting-child', 6)
        self.assertIn(budget_key, row.budget_keys)
        self.assertIsNone(db.session.get(AIExecution, 'blocked-child'))
        budget = db.session.get(AIBudget, budget_key)
        self.assertEqual((budget.used_tokens, budget.reserved_tokens), (4, 6))

    def test_concurrent_execution_sends_once(self):
        started, finish = threading.Event(), threading.Event()
        binding = dict(self.binding)
        def respond(*args,**kwargs):
            started.set(); finish.wait(10); return self.response()
        def work():
            with self.app.app_context():
                result=run_skill('summarize-university-notice','summary',EVIDENCE,'summary','concurrent',binding=binding)
                db.session.remove(); return result
        db.session.remove()
        with patch('backend.ai.providers.complete',side_effect=respond) as transport, ThreadPoolExecutor(max_workers=4) as pool:
            initial=pool.submit(work); self.assertTrue(started.wait(10))
            duplicates=[pool.submit(work) for _ in range(3)]
            self.assertTrue(all(f.result(timeout=10)['status']=='pending' for f in duplicates))
            finish.set(); self.assertEqual(initial.result(timeout=10)['status'],'succeeded')
            self.assertEqual(transport.call_count,1)

    def test_unknown_result_keeps_reservation_and_never_replays(self):
        with patch('backend.ai.providers.complete',side_effect=ProviderError('timeout',uncertain=True)) as transport:
            self.assertEqual(self.invoke()['status'],'uncertain')
            self.assertEqual(self.invoke()['status'],'uncertain')
            self.assertEqual(transport.call_count,1)
        self.assertTrue(all(b.reserved_tokens>0 and b.active_count==0 for b in AIBudget.query.all()))
        reconcile_usage('one',80)
        self.assertTrue(all(b.used_tokens==80 and b.reserved_tokens==0 for b in AIBudget.query.all()))
        reconcile_usage('one',80)
        self.assertTrue(all(b.used_tokens==80 for b in AIBudget.query.all()))

    def test_invalid_output_is_billed_but_never_succeeds(self):
        invalid=deepcopy(OUTPUT);invalid['facts'][0]['evidence_ids']=['invented']
        with patch('backend.ai.providers.complete',return_value=self.response(invalid)):
            result=self.invoke()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error_code'],'output_validation_failed')
        self.assertIsNone(result['output'])
        self.assertEqual(AIBudget.query.first().used_tokens,70)

    def test_optional_budget_blocks_before_network(self):
        save_limits({'total':1})
        with patch('backend.ai.providers.complete') as transport:
            with self.assertRaises(AIConfigError): self.invoke()
            transport.assert_not_called()
        self.assertEqual(AIExecution.query.count(),0)
        save_limits({'total':0})
        with patch('backend.ai.providers.complete',return_value=self.response()):
            self.assertEqual(self.invoke()['status'],'succeeded')

    def test_revoked_profile_cannot_dispatch_queued_binding(self):
        delete_profile(self.binding['id'])
        with patch('backend.ai.providers.complete') as transport:
            with self.assertRaises(AIConfigError): self.invoke()
            transport.assert_not_called()

    def test_deleted_profile_leaves_settings_but_preserves_billing(self):
        with patch('backend.ai.providers.complete', return_value=self.response()):
            self.invoke()
        models.AppConfig.set('deepseek_api_key', 'fixture-legacy-key')
        delete_profile(self.binding['id'])
        self.assertEqual(public_settings()['profiles'], [])
        self.assertEqual(public_settings()['bindings'], {})
        self.assertIsNone(public_settings()['legacy'])
        self.assertEqual(db.session.get(AIExecution, 'one').usage['total_tokens'], 70)
        self.assertTrue(all(b.used_tokens == 70 for b in AIBudget.query.all()))
        with self.assertRaises(AIConfigError):
            get_model_binding('summary')
        with self.assertRaises(AIConfigError):
            save_profile({'api_key': 'replacement-fixture-key'}, self.binding['id'])
        with self.assertRaises(AIConfigError):
            test_connection(self.binding['id'])

    def test_disabled_profile_can_be_deleted_and_is_never_reused(self):
        save_profile({'enabled': False}, self.binding['id'])
        self.assertTrue(public_settings()['profiles'][0]['has_key'])
        delete_profile(self.binding['id'])
        self.assertEqual(public_settings()['profiles'], [])
        new = save_profile({'provider': 'deepseek', 'model': 'deepseek-chat', 'api_key': 'new-fixture-key'})
        self.assertNotEqual(new['id'], self.binding['id'])
        with patch('backend.ai.providers.complete') as transport:
            with self.assertRaises(AIConfigError): self.invoke()
            transport.assert_not_called()

    def test_disable_enable_keeps_key_validation_and_bindings_without_paid_calls(self):
        profile_id = self.binding['id']
        encrypted = db.session.get(AIProfile, profile_id).encrypted_key
        bindings = public_settings()['bindings']
        with patch('backend.ai.providers.complete') as transport:
            disabled = save_profile({'enabled': False, 'expected_version': self.binding['version']}, profile_id)
            self.assertFalse(disabled['enabled'])
            self.assertTrue(disabled['has_key'])
            self.assertTrue(disabled['tested'])
            self.assertEqual(public_settings()['bindings'], bindings)
            self.assertEqual(db.session.get(AIProfile, profile_id).encrypted_key, encrypted)
            with self.assertRaises(AIConfigError): get_model_binding('summary')
            with self.assertRaises(AIConfigError): self.invoke()
            self.assertEqual(credential_for(disabled, allow_untested=True), 'never-a-real-key')
            enabled = save_profile({'enabled': True, 'expected_version': disabled['version']}, profile_id)
            self.assertTrue(enabled['enabled'])
            self.assertTrue(enabled['tested'])
            self.assertEqual(get_model_binding('summary')['id'], profile_id)
            self.assertEqual(get_model_binding('directory')['id'], profile_id)
            self.assertEqual(credential_for(enabled), 'never-a-real-key')
            self.assertEqual(db.session.get(AIProfile, profile_id).encrypted_key, encrypted)
            with self.assertRaises(AIConfigError): credential_for(self.binding)
            transport.assert_not_called()

    def test_disable_during_connection_test_stays_disabled(self):
        def respond(*args, **kwargs):
            save_profile({'enabled': False}, self.binding['id'])
            return self.response({'ok': True})
        with patch('backend.ai.providers.complete', side_effect=respond):
            test_connection(self.binding['id'])
        profile = public_settings()['profiles'][0]
        self.assertFalse(profile['enabled'])
        self.assertTrue(profile['has_key'])
        self.assertTrue(profile['tested'])

    def test_enable_requires_tested_config_and_boolean_state(self):
        profile_id = self.binding['id']
        with self.assertRaises(AIConfigError): save_profile({'enabled': 'false'}, profile_id)
        save_profile({'model': 'untested-model'}, profile_id)
        with self.assertRaises(AIConfigError): save_profile({'enabled': True}, profile_id)


    def test_legacy_summary_key_does_not_authorize_new_directory_spend(self):
        AIBinding.query.delete(); AIProfile.query.delete(); db.session.commit()
        models.AppConfig.set('deepseek_api_key','fixture-legacy-key')
        self.assertEqual(get_model_binding('summary')['id'],0)
        with patch('backend.ai.providers.complete') as transport:
            with self.assertRaises(AIConfigError) as error:
                get_model_binding('directory')
            self.assertEqual(error.exception.code,'not_configured')
            transport.assert_not_called()

    def test_configuration_update_requires_test_and_does_not_dispatch(self):
        row=save_profile({'model':'another-model'},self.binding['id'])
        self.assertFalse(row['enabled']);self.assertFalse(row['tested'])
        with self.assertRaises(AIConfigError):get_model_binding('summary')

    def test_connection_test_checks_usage_and_only_enables_same_version(self):
        profile=db.session.get(AIProfile,self.binding['id']);profile.enabled=False;db.session.commit()
        with patch('backend.ai.providers.complete',return_value=self.response({'ok':True})):
            result=test_connection(profile.id)
        self.assertTrue(result['tested'])
        self.assertTrue(db.session.get(AIProfile,profile.id).enabled)

    def test_interrupted_call_is_fenced_without_releasing_unknown_spend(self):
        with patch('backend.ai.providers.complete',side_effect=ProviderError('unknown',uncertain=True)):
            self.invoke()
        row=db.session.get(AIExecution,'one');row.status='sending';row.active_released=False
        row.created_at=datetime.utcnow()-timedelta(minutes=5)
        row.heartbeat_at=row.created_at
        for budget in AIBudget.query.all():budget.active_count=1
        db.session.commit()
        self.assertEqual(recover_uncertain_executions(),1)
        self.assertEqual(db.session.get(AIExecution,'one').status,'uncertain')
        self.assertTrue(all(b.active_count==0 and b.reserved_tokens>0 for b in AIBudget.query.all()))

    def test_old_call_with_live_heartbeat_is_not_interrupted(self):
        with patch('backend.ai.providers.complete',side_effect=ProviderError('unknown',uncertain=True)):
            self.invoke()
        row=db.session.get(AIExecution,'one');row.status='sending';row.active_released=False
        row.created_at=datetime.utcnow()-timedelta(minutes=8)
        row.heartbeat_at=datetime.utcnow()
        db.session.commit()
        self.assertEqual(recover_uncertain_executions(),0)
        self.assertEqual(row.status,'sending')

    def test_execution_identity_cannot_be_reused_for_other_input(self):
        with patch('backend.ai.providers.complete',return_value=self.response()):self.invoke()
        with self.assertRaises(AIConfigError):
            run_skill('summarize-university-notice','summary',{**EVIDENCE,'title':'other'},'summary','one',binding=self.binding)


class SkillContractTests(unittest.TestCase):
    def test_all_resources_load_from_package_without_personal_paths(self):
        for skill,modes in [('university-source-onboarding',['classify','extraction','review']),
                            ('summarize-university-notice',['summary','extract','synthesize'])]:
            for mode in modes:
                loaded=load_skill(skill,mode)
                self.assertEqual(len(loaded.resource_digest),64)
                self.assertTrue(loaded.output_schema)
        with self.assertRaises(SkillValidationError):load_skill('../outside','summary')

    def test_summary_rejects_false_coverage_invented_dates_and_missing_evidence(self):
        skill=load_skill('summarize-university-notice','summary')
        validate_input(skill,EVIDENCE);validate_output(skill,OUTPUT,EVIDENCE)
        for invalid in [dict(OUTPUT,summary='截止2029年9月30日'),
                        dict(OUTPUT,coverage={'paragraph_ids':[],'attachments_included':False}),
                        dict(OUTPUT,coverage={'paragraph_ids':['p1'],'attachments_included':True})]:
            with self.assertRaises(SkillValidationError):validate_output(skill,invalid,EVIDENCE)

    def test_source_relationship_cycles_and_unknown_evidence_rejected(self):
        evidence={'school_id':1,'entities':[{'id':'school:1'}],
                  'candidates':[{'candidate_id':'a'},{'candidate_id':'b'}],
                  'evidence':[{'evidence_id':'official','text':'官方机构名录'}]}
        def unit(ident,parent):
            return {'candidate_id':ident,'kind':'unit','name':ident,'parent_entity_id':parent,
                    'publisher_entity_id':None,'topics':[],'decision':'propose',
                    'evidence':{'identity':['official'],'name':['official'],'parent':['official'],'publisher':[]},
                    'reason':'官方名录','needed_evidence':[]}
        output={'school_id':1,'results':[unit('a','b'),unit('b','a')],'coverage_gaps':[]}
        with self.assertRaises(SkillValidationError):validate_output(load_skill('university-source-onboarding','classify'),output,evidence)
        output['results'][1]['parent_entity_id']='school:1'
        validate_output(load_skill('university-source-onboarding','classify'),output,evidence)
        output['results'][0]['evidence']['identity']=['invented']
        with self.assertRaises(SkillValidationError):validate_output(load_skill('university-source-onboarding','classify'),output,evidence)


class FakeResponse:
    def __init__(self,payload,status=200):self.payload,self.status_code,self.headers=payload,status,{}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def iter_content(self,chunk_size):yield json.dumps(self.payload).encode()


class OfficialProviderTests(unittest.TestCase):
    def test_four_official_protocols_parse_content_and_usage(self):
        for provider,region in [('deepseek','default'),('dashscope','cn-beijing'),('ark','cn-beijing'),('bigmodel','default')]:
            choice={'message':{'content':'{"ok":true}'},'finish_reason':'stop'}
            payload={'choices':[choice],'usage':{'prompt_tokens':3,'completion_tokens':2},'id':'r'}
            if provider=='dashscope':payload={'output':{'choices':[choice]},'usage':{'input_tokens':3,'output_tokens':2},'request_id':'r'}
            with patch('backend.ai.providers.requests.Session') as factory:
                session=factory.return_value;session.post.return_value=FakeResponse(payload)
                result=complete({'provider':provider,'region':region,'model':'fixture'},'dummy-secret',[{'role':'user','content':'JSON'}])
                self.assertEqual(result.usage['total_tokens'],5)
                kwargs=session.post.call_args.kwargs
                self.assertFalse(kwargs['allow_redirects']);self.assertEqual(kwargs['timeout'],(8,60))
                self.assertFalse(session.trust_env);self.assertEqual(session.post.call_count,1)
                if provider=='dashscope':self.assertIn('input',kwargs['json'])
                else:self.assertIn('messages',kwargs['json'])
                if provider=='deepseek':
                    self.assertEqual(kwargs['json'].get('thinking'), {'type': 'disabled'})

    def test_redirect_and_arbitrary_endpoint_rejected(self):
        with patch('backend.ai.providers.requests.Session') as factory:
            factory.return_value.post.return_value=FakeResponse({},302)
            with self.assertRaises(ProviderError) as error:
                complete({'provider':'deepseek','region':'default','model':'x'},'dummy',[])
            self.assertEqual(error.exception.code,'redirect_rejected')
            self.assertEqual(factory.return_value.post.call_count,1)
        with self.assertRaises(ValueError):
            complete({'provider':'deepseek','region':'http://localhost/','model':'x'},'dummy',[])
