"""Source configuration history records actual commits, never inferred owners."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine,text
from alembic.migration import MigrationContext
from alembic.operations import Operations

from backend.database.db import db
from backend.database.models import BackgroundTask,Department,School
from backend.database.source_governance_models import SourceConfigVersion,SourceValidation
from backend.services import tasks
from backend.services.source_onboarding import onboard_page
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.structure_snapshot import read_structure,graph_projection
from backend.services.structure_versions import current_version
from tests import test_source_attention_flow as attention_fixture

TEACHER_TITLES,OTHER_TITLES=attention_fixture.TEACHER_TITLES,attention_fixture.OTHER_TITLES


class SourceExecutionEventTests(unittest.TestCase):
    def setUp(self):
        self.flow=attention_fixture.SourceAttentionFlowTests();self.flow.setUp();self.addCleanup(self.flow.doCleanups)
        self.app=self.flow.f.app
        self.school=db.session.get(School,self.flow.f.school_id)
        RuntimeCatalog(self.flow.f.app.config['SOURCE_CATALOG_PATH']).publish(self.flow.inv,self.flow.key)

    def fetch(self,url,purpose):
        if url in self.flow.pages:return self.flow.pages[url]
        titles=TEACHER_TITLES if '/teacher/' in url else OTHER_TITLES
        title=titles[int(url.rsplit('/',1)[-1].split('.')[0])]
        return '<article><h1>'+title+'</h1><p>请有关单位按照学校公布的具体安排组织开展相关活动，详细事项和时间见学校官方网站通知正文。</p></article>'

    def connect(self):
        self.flow.queue();handle=tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle),patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model call')):
            result=onboard_page(handle['payload'],fetcher=self.fetch)
        tasks.finish(handle,result)
        self.assertEqual(result['state'],'connected')
        return SourceConfigVersion.query.order_by(SourceConfigVersion.id.desc()).first()

    def test_native_commit_records_source_identity_and_exact_page_ancestry(self):
        version=self.connect()
        record=version.execution_json
        self.assertEqual(record['event_type'],'source_configuration_saved')
        self.assertEqual(record['config_version_id'],version.id)
        self.assertEqual(record['source_id'],version.department_id)
        self.assertEqual(record['config_hash'],version.config_hash)
        self.assertTrue(record['parent_event_id'])
        self.assertFalse(record['establishes_ownership'])
        self.assertTrue(record['page_receipt']['events'])
        from backend.scraper.discovery.parser_revision import PARSER_REVISION
        self.assertEqual(record['parser_revision'],PARSER_REVISION)
        self.assertEqual(record['recorder_version'],1)
        graph=graph_projection(read_structure(self.school),self.school)
        event=next(event for event in graph['exploration_events'] if event['id']==record['id'])
        self.assertEqual(event['node_id'],f'source:{version.department_id}')
        self.assertEqual(event['stream_id'],'source_config_versions')
        self.assertEqual(event['sequence'],version.id)
        self.assertTrue(event['current_configuration'])
        self.assertTrue(any(event['id']==record['parent_event_id'] for event in graph['exploration_events']))
        self.assertFalse(any(edge['type']=='source_ownership' for edge in graph['relations']))

    def test_failed_final_source_commit_publishes_neither_source_nor_event(self):
        self.flow.queue();handle=tasks.claim(capabilities=['http'])
        before=current_version(self.school.id)
        with tasks.execution_scope(handle),patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model call')), \
             patch('backend.services.school_structure.sync_official_structure',side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError,'fixture failure'):
                onboard_page(handle['payload'],fetcher=self.fetch)
        db.session.rollback()
        self.assertEqual(SourceConfigVersion.query.count(),0)
        self.assertEqual(SourceValidation.query.filter(SourceValidation.department_id.is_not(None)).count(),0)
        self.assertEqual(current_version(self.school.id),before)
        self.assertFalse(any(event['event_type']=='source_configuration_saved' for event in graph_projection(read_structure(self.school),self.school)['exploration_events']))

    def test_receipt_survives_later_page_window_loss_without_inventing_history(self):
        version=self.connect();capture=read_structure(self.school)
        capture['report']['exploration_events']=[]
        graph=graph_projection(capture,self.school)
        event=next(event for event in graph['exploration_events'] if event['node_id']==f'source:{version.department_id}')
        self.assertIsNotNone(event['parent_event_id'])
        self.assertTrue(any(e['id']==event['parent_event_id'] for e in graph['exploration_events']))
        self.assertEqual(graph['source_count'],1)

    def test_corrupt_parent_receipt_does_not_become_a_complete_source_path(self):
        version=self.connect();record=deepcopy(version.execution_json)
        record['page_receipt']['events'][0]['label']='corrupted ancestor'
        version.execution_json=record;db.session.commit()
        graph=graph_projection(read_structure(self.school),self.school)
        event=next(event for event in graph['exploration_events'] if event['node_id']==f'source:{version.department_id}')
        self.assertIsNone(event['parent_event_id'])
        self.assertEqual(event['upstream_status'],'invalid_page_receipt')
        self.assertEqual(graph['source_count'],1,'A history gap must not revoke the actual saved source')

    def test_legacy_config_versions_are_not_backfilled_as_connection_events(self):
        version=self.connect();version.execution_json=None;db.session.commit()
        graph=graph_projection(read_structure(self.school),self.school)
        self.assertFalse(any(event['node_id']==f'source:{version.department_id}' for event in graph['exploration_events']))
        self.assertEqual(graph['exploration_history']['legacy_source_configurations'],1)
        self.assertEqual(graph['source_count'],1)

    def test_non_transactional_department_edit_does_not_reuse_old_config_receipt_as_current(self):
        version=self.connect()
        source=db.session.get(Department,version.department_id)
        source.name='Renamed without saving a configuration version';db.session.commit()
        graph=graph_projection(read_structure(self.school),self.school)
        event=next(event for event in graph['exploration_events'] if event['node_id']==f'source:{source.id}')
        self.assertFalse(event['current_configuration'])

    def test_changed_validation_material_is_not_linked_to_an_old_inspection(self):
        self.flow.pages[attention_fixture.TEACHER]+='<p>Changed after scheduling</p>'
        self.flow.queue()
        queued=BackgroundTask.query.filter_by(kind='onboard').order_by(BackgroundTask.id).first()
        # Normal scheduling reuses the observed snapshot, which is still valid.
        # Force a real new read to exercise changed validation material.
        queued.payload=dict(queued.payload,snapshot=None);db.session.commit()
        version=self.connect()
        self.assertIsNone(version.execution_json['parent_event_id'])
        self.assertEqual(version.execution_json['upstream_status'],'material_changed')
        self.assertIsNone(version.execution_json['page_receipt'])
        self.assertEqual(graph_projection(read_structure(self.school),self.school)['source_count'],1)

    def test_history_changes_share_structure_revision_and_noop_does_not_increment(self):
        version=self.connect();before=current_version(self.school.id)
        record=deepcopy(version.execution_json);record['writer']='corrected_actual_writer'
        version.execution_json=record;db.session.commit()
        after=current_version(self.school.id)
        self.assertEqual(after['revision'],before['revision']+1)
        db.session.commit()
        self.assertEqual(current_version(self.school.id),after)

    def test_foreign_school_marker_is_rejected_without_removing_source(self):
        version=self.connect();record=deepcopy(version.execution_json)
        record['school_id']+=100;version.execution_json=record;db.session.commit()
        graph=graph_projection(read_structure(self.school),self.school)
        self.assertFalse(any(e['event_type']=='source_configuration_saved' for e in graph['exploration_events']))
        self.assertEqual(graph['exploration_history']['invalid_source_receipts'],1)
        self.assertEqual(graph['source_count'],1)

    def test_full_backup_restore_keeps_exact_receipt_and_resets_only_sync_epoch(self):
        from backend import create_app
        from backend.services.backups import create_backup,restore_backup
        version=self.connect();expected=deepcopy(version.execution_json)
        source_id=version.department_id;school_id=self.school.id
        before=current_version(school_id)
        root=Path(self.flow.f.temp.name)
        self.flow.f.app.config['BACKUP_DIR']=str(root/'backups')
        backup=create_backup()
        destination=root/'restored'
        restore_backup(root/'backups'/backup['backup'],destination)
        restored=create_app({'TESTING':True,'SECRET_KEY':'receipt-restore',
            'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(destination/'test.db'),
            'SOURCE_CATALOG_PATH':str(destination/'catalog.db'),
            'DISCOVERY_CACHE_PATH':str(destination/'discovery.db'),
            'SOURCE_GOVERNANCE_EVIDENCE_PATH':str(destination/'source-governance-evidence')})
        with restored.app_context():
            try:
                self.assertEqual(SourceConfigVersion.query.one().execution_json,expected)
                self.assertNotEqual(current_version(school_id)['epoch'],before['epoch'])
                school=db.session.get(School,school_id)
                graph=graph_projection(read_structure(school),school)
                event=next(e for e in graph['exploration_events'] if e['node_id']==f'source:{source_id}')
                self.assertTrue(event['current_configuration'])
                self.assertIn(event['parent_event_id'],{e['id'] for e in graph['exploration_events']})
            finally:
                db.session.remove();db.engine.dispose()

    def test_truncated_receipt_preserves_gap_instead_of_using_current_navigation(self):
        from backend.services.source_execution_events import capture_page_receipt
        self.flow.queue()
        queued=BackgroundTask.query.filter_by(kind='onboard').order_by(BackgroundTask.id).first()
        context=deepcopy(queued.payload['discovery_attention'])
        actual=self.flow.inv.report(self.flow.key)['exploration_events']
        root_ids={event['id'] for event in actual if event['event_type']=='exploration_started'}
        context['inspection_receipt']=capture_page_receipt(
            {event['id']:event for event in actual if event['id'] not in root_ids},
            context['inspection_event_id'],self.flow.key)
        self.assertTrue(context['inspection_receipt']['truncated'])
        queued.payload=dict(queued.payload,discovery_attention=context);db.session.commit()
        self.connect();capture=read_structure(self.school)
        capture['report']['exploration_events']=[]
        graph=graph_projection(capture,self.school)
        self.assertTrue(graph['exploration_history']['missing_projected_parent_ids'])
        self.assertFalse(root_ids & {event['id'] for event in graph['exploration_events']})
        self.assertTrue(any(edge['type']=='navigation_link' for edge in graph['relations']))

    def test_conflicting_retained_and_published_event_is_omitted_with_explicit_gap(self):
        self.connect();capture=read_structure(self.school)
        event=capture['report']['exploration_events'][0]
        event['label']='Conflicting version of the same event'
        graph=graph_projection(capture,self.school)
        self.assertIn(event['id'],graph['exploration_history']['conflicting_event_ids'])
        self.assertNotIn(event['id'],{e['id'] for e in graph['exploration_events']})
        self.assertIn(event['id'],graph['exploration_history']['missing_projected_parent_ids'])
        self.assertEqual(graph['source_count'],1)


class ProposalExecutionEventTests(unittest.TestCase):
    def setUp(self):
        from tests import test_source_governance as proposal_fixture
        self.f=proposal_fixture.SourceGovernanceTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups);self.addCleanup(self.f.tearDown)

    def validated(self):
        from backend.services import source_governance as governance
        proposal=self.f.proposal()
        self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
        return proposal

    def test_proposal_commit_has_its_own_config_identity_but_no_inferred_upstream(self):
        from backend.services import source_governance as governance
        proposal=self.validated();before=current_version(self.f.school.id)
        source=governance.activate_proposal(proposal.id)
        version=SourceConfigVersion.query.one()
        self.assertEqual(version.execution_json['writer'],'validated_proposal')
        self.assertIsNone(version.execution_json['parent_event_id'])
        self.assertEqual(version.execution_json['upstream_status'],'not_recorded')
        # This older proposal fixture uses Inventory as its catalogue adapter;
        # the snapshot reader deliberately requires a published RuntimeCatalog.
        catalog=RuntimeCatalog(self.f.app.config['SOURCE_CATALOG_PATH'])
        with patch('backend.services.runtime_catalog.published_catalog',return_value=catalog):
            graph=graph_projection(read_structure(self.f.school),self.f.school)
        event=next(e for e in graph['exploration_events'] if e['node_id']==f'source:{source.id}')
        self.assertTrue(event['current_configuration'])
        after=current_version(self.f.school.id)
        self.assertEqual(after['revision'],before['revision']+1)
        governance.activate_proposal(proposal.id)
        self.assertEqual(SourceConfigVersion.query.count(),1)
        self.assertEqual(current_version(self.f.school.id),after)

    def test_activation_failure_after_receipt_flush_rolls_back_receipt_and_revision(self):
        from backend.services import source_governance as governance
        proposal=self.validated();before=current_version(self.f.school.id)
        def fail_after_receipt(_):
            self.assertIsNotNone(SourceConfigVersion.query.one().execution_json)
            raise RuntimeError('fixture activation failure')
        with patch('backend.services.directory_work.resolve_column_material',side_effect=fail_after_receipt):
            with self.assertRaisesRegex(RuntimeError,'fixture activation failure'):
                governance.activate_proposal(proposal.id)
        db.session.rollback()
        self.assertEqual(SourceConfigVersion.query.count(),0)
        self.assertEqual(current_version(self.f.school.id),before)
        self.assertEqual(proposal.state,'validated')


class SourceExecutionMigrationTests(unittest.TestCase):
    def test_nullable_upgrade_and_downgrade_keep_legacy_configuration_rows(self):
        engine=create_engine('sqlite:///:memory:')
        with engine.begin() as connection:
            connection.execute(text('CREATE TABLE source_config_versions (id INTEGER PRIMARY KEY, config_json TEXT NOT NULL)'))
            connection.execute(text("INSERT INTO source_config_versions VALUES(7,'retained config')"))
            path=Path(__file__).parents[1]/'migrations/versions/49fb21567ed4_source_execution_receipts.py'
            spec=importlib.util.spec_from_file_location('source_execution_migration',path)
            migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                self.assertEqual(tuple(connection.execute(text('SELECT id,config_json,execution_json FROM source_config_versions')).one()),(7,'retained config',None))
                migration.downgrade()
            self.assertEqual(tuple(connection.execute(text('SELECT * FROM source_config_versions')).one()),(7,'retained config'))
        engine.dispose()


if __name__=='__main__':unittest.main()
