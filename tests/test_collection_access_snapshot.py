"""Durable entry observations update every shared reader without admitting sources."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations

from backend.database.db import db
from backend.database.models import BackgroundTask, ScrapeLog
from backend.database.structure_version_models import SourceAccessObservation
from backend.scraper.acquisition import FetchRequest, FetchResult
from backend.services import tasks, task_fetch
from backend.services.collection_access import read_entry_observations, save_entry_observation
from backend.services.source_channels import channels_for
from backend.services.source_governance import _hash, source_config
from backend.services.structure_snapshot import read_structure, source_choices
from backend.services.structure_versions import current_version
from tests import test_shared_page_mutations as shared_fixture


class CollectionAccessSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.f=shared_fixture.SharedPageMutationTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.app=self.f.f.fixture.app
        self.app.config['FETCH_EVIDENCE_DIR']=str(Path(self.app.config['SOURCE_CATALOG_PATH']).parent/'fetch-evidence')
        self.source=self.f.first
        job=tasks.enqueue('collect',self.source.id,{'school_id':self.source.school_id,'department_id':self.source.id})
        db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id!=job.id)
            .values(available_at=datetime.utcnow()+timedelta(days=1)))
        db.session.commit()
        self.handle=tasks.claim();self.assertEqual(self.handle['id'],job.id)
        self.log=ScrapeLog(school_id=self.source.school_id,source_name=self.source.name,
            task_id=self.handle['id'],task_generation=self.handle['generation'],task_token=self.handle['token'])
        db.session.add(self.log);db.session.commit()
        config=source_config(self.source)
        self.context=dict(schema_version=1,log_id=self.log.id,task_id=self.handle['id'],
            task_generation=self.handle['generation'],school_id=self.source.school_id,source_id=self.source.id,
            config_hash=_hash(config),config=config)
        self.stamp=datetime.now(timezone.utc)+timedelta(hours=1)
        self.request=FetchRequest(self.source.list_url,purpose='list',source_id=str(self.source.id),browser_allowed=False)

    def result(self,status=403,offset=0):
        return FetchResult(self.source.list_url,status=status,outcome='usable' if status==200 else 'denied',
            error_code='' if status==200 else f'http_{status}',
            observed_at=(self.stamp+timedelta(seconds=offset)).isoformat())

    def save(self,result,request=None,context=None):
        with tasks.execution_scope(self.handle):
            return save_entry_observation(context or self.context,request or self.request,result)

    def source_node(self,graph):
        return next(n for n in graph['nodes'] if n['id']==f'source:{self.source.id}')

    def test_entry_failure_updates_shared_views_delta_and_survives_cache_cleanup(self):
        before=self.f.get(); retained=self.f.retained()
        with tasks.execution_scope(self.handle), task_fetch.collection_observation_context(self.source,self.log):
            task_fetch.record_collection_observation(self.request,self.result(404))
        after=self.f.get(); node=self.source_node(after)
        self.assertEqual((node['access'],node['state'],node['status_code']),('not_found','connected',404))
        self.assertEqual(after['relations'],before['relations'])
        self.assertEqual({n['id'] for n in after['nodes']},{n['id'] for n in before['nodes']})
        with self.app.test_request_context('/'):
            capture=read_structure(self.f.f.school)
            choice=next(c for groups in source_choices(capture).values() for group in groups
                for c in group['own_columns'] if c['department'].id==self.source.id)
            descriptor=channels_for([self.source])[self.source.id]
            for view in (choice,descriptor):
                self.assertEqual(view['access_observation'],node['access_observation'])
        delta=self.f.get(since=before['structure_token'])
        self.assertEqual(delta['update_mode'],'delta')
        self.assertTrue(any(n['id']==node['id'] and n['access']=='not_found' for n in delta['delta']['nodes']['upsert']))
        self.assertEqual(self.f.retained(),retained)
        path=task_fetch.collection_observation_path(self.log)
        self.assertTrue(path.is_file())
        tasks.finish(self.handle,result={})
        task_fetch.prune_fetch_evidence(all_cache=True)
        self.assertFalse(path.exists())
        self.assertEqual(self.source_node(self.f.get())['access_observation'],node['access_observation'])

    def test_body_other_list_pdf_and_wrong_identity_cannot_replace_entry(self):
        self.assertTrue(self.save(self.result(200)))
        before=current_version(self.source.school_id)
        other_requests=[FetchRequest(self.source.list_url+'/body',purpose='article'),
            FetchRequest(self.source.list_url+'?page=2',purpose='list'),
            FetchRequest(self.source.list_url,purpose='list',source_id='99999'),
            FetchRequest(self.source.list_url,purpose='list',policy={'embedded_pdf_child':True})]
        for request in other_requests:
            self.assertFalse(self.save(self.result(404,1),request))
        for key,value in (('school_id',99999),('config_hash','old'),('source_id',self.f.second.id)):
            context=deepcopy(self.context);context[key]=value
            self.assertFalse(self.save(self.result(403,1),context=context))
        self.assertFalse(self.save(FetchResult(self.source.list_url,status=403,outcome='denied')))
        self.assertEqual(current_version(self.source.school_id),before)
        self.assertEqual(self.source_node(self.f.get())['access'],'responded')

    def test_duplicate_older_and_equal_time_results_remain_truthful(self):
        self.assertTrue(self.save(self.result(403)))
        version=current_version(self.source.school_id)
        self.assertFalse(self.save(self.result(403)))
        self.assertFalse(self.save(self.result(200,-1)))
        self.assertEqual(current_version(self.source.school_id),version)
        self.assertTrue(self.save(self.result(200)))
        self.assertEqual(self.source_node(self.f.get())['access'],'conflicting')
        self.assertTrue(self.save(self.result(503,1)))
        node=self.source_node(self.f.get())
        self.assertEqual((node['access'],node['state']),('failed','connected'))
        record=SourceAccessObservation.query.one()
        self.assertEqual(len(json.loads(record.observation_json)['records']),1)
        network=FetchResult(self.source.list_url,outcome='network_error',error_code='http_connect_timeout',
            observed_at=(self.stamp+timedelta(seconds=2)).isoformat())
        self.assertTrue(self.save(network))
        node=self.source_node(self.f.get())
        self.assertEqual((node['access'],node['status_code'],node['state']),('failed',None,'connected'))

    def test_lost_fence_rolls_back_observation_and_structure_version(self):
        before=current_version(self.source.school_id)
        with patch.object(tasks,'checkpoint',side_effect=tasks.LeaseLost('lost generation')):
            with self.assertRaises(tasks.LeaseLost):
                self.save(self.result())
        db.session.rollback()
        self.assertEqual(SourceAccessObservation.query.count(),0)
        self.assertEqual(current_version(self.source.school_id),before)

    def test_cache_reuse_keeps_the_original_observation_and_does_not_bump_revision(self):
        self.assertTrue(self.save(self.result(200)))
        before=current_version(self.source.school_id)
        with tasks.execution_scope(self.handle):
            self.assertFalse(save_entry_observation(self.context,self.request,self.result(200),replay_ref='same-cache.json'))
        self.assertEqual(current_version(self.source.school_id),before)
        records=read_entry_observations(self.source.school_id)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['reference']['observed_at'],self.result(200).observed_at)

    def test_rule_change_at_same_url_cannot_borrow_old_access_record(self):
        self.assertTrue(self.save(self.result(200)))
        self.source.title_selector='a.updated-rule'
        db.session.commit()
        self.assertFalse(self.save(self.result(403,1)))
        capture=read_structure(self.f.f.school)
        node=next(n for n in capture['facts']['nodes'] if n['id']==f'source:{self.source.id}')
        self.assertEqual(node['access'],'unknown')
        self.assertEqual(node['access_observations'],[])

    def test_additive_migration_and_unmigrated_reads_preserve_existing_material(self):
        retained=self.f.retained()
        SourceAccessObservation.__table__.drop(db.engine)
        self.assertEqual(read_entry_observations(self.source.school_id),[])
        before=self.f.get()
        self.assertFalse(self.save(self.result()))
        module_path=Path(__file__).resolve().parents[1]/'migrations/versions/6b1d43789af6_source_entry_observations.py'
        spec=importlib.util.spec_from_file_location('entry_observation_migration',module_path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with db.engine.begin() as connection:
            with patch.object(module,'op',Operations(MigrationContext.configure(connection))):
                module.upgrade()
        self.assertEqual(read_entry_observations(self.source.school_id),[])
        after=self.f.get()
        self.assertEqual(after['nodes'],before['nodes'])
        self.assertEqual(after['relations'],before['relations'])
        self.assertEqual(self.f.retained(),retained)
        with db.engine.begin() as connection:
            with patch.object(module,'op',Operations(MigrationContext.configure(connection))):
                module.downgrade()
        self.assertEqual(read_entry_observations(self.source.school_id),[])
