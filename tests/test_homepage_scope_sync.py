"""A saved preview review and its structure cursor commit or roll back together."""
from contextlib import ExitStack
import gzip
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.orm import Session

from backend.database.db import db
from backend.database.source_governance_models import SourceConfigVersion, SourceReviewEvent
from backend.services import source_governance as g
from backend.services.source_scope import hold_configured_joint_scope,publish_configured_scope
from backend.services.structure_snapshot import read_structure, source_choices
from backend.services.structure_versions import current_version
from tests.test_homepage_scope_modules import preview_config, preview_html


class HomepagePreviewSyncTests(unittest.TestCase):
    def setUp(self):
        from backend.services.source_inventory import Inventory
        self.inventory_init=Inventory.__init__
        from tests.test_structure_snapshot import StructureSnapshotTests
        self.fixture=StructureSnapshotTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.guards=ExitStack();self.addCleanup(self.guards.close)
        self.guards.enter_context(patch('socket.getaddrinfo',side_effect=AssertionError('No DNS')))
        self.guards.enter_context(patch('socket.socket.connect',side_effect=AssertionError('No network')))
        self.school=self.fixture.school
        self.fixture.app.config['SOURCE_GOVERNANCE_EVIDENCE_PATH']=str(
            Path(self.fixture.app.config['SOURCE_CATALOG_PATH']).parent/'preview-review-evidence')
        self.config=dict(preview_config(),list_url=self.school.url.rstrip('/')+'/preview/')
        self.source=self.fixture.source(self.config['name'],url=self.config['list_url'],selector=self.config['list_selector'])
        for field,value in self.config.items():setattr(self.source,field,value)
        db.session.commit()
        html=preview_html()
        page=dict(url=self.config['list_url'],label='主页预览',kind='navigation',state='fetched',
            content_hash=g._hash(html),checked_at='2026-10-30T03:00:00+00:00')
        self.fixture.fixture.publish([page])
        with self.fixture.fixture.catalog.connect(write=True)as connection:
            connection.execute('UPDATE catalog_pages SET evidence_gzip=? WHERE url=?',
                (gzip.compress(html.encode()),self.config['list_url']))
        reference=g._snapshot(html,self.config['list_url'])
        reference.update(observed_at=page['checked_at'],captured_at=page['checked_at'])
        bundle=dict(schema=1,school_id=self.school.id,school_name=self.school.name,root_url=self.school.url,
            config=self.config,config_hash=g._hash(self.config),expected_config=self.config,
            list=reference,articles=[],identity_paths=[],identity_snapshots=[])
        self.proposal=g.propose_source(self.school.id,self.config,g.CapturedEvidence(bundle),
            department_id=self.source.id,origin='preview_sync_incomplete_list_only')

    def source_node(self,graph):
        return next(node for node in graph['nodes']if node['id']==f'source:{self.source.id}')

    def exploration(self,**query):
        return self.fixture.get(scope='exploration',**query)

    def publish_scope(self):
        # Material publication uses a local inventory; passive reads still forbid it.
        with patch('backend.services.source_inventory.Inventory.__init__',self.inventory_init):
            return publish_configured_scope(self.proposal.id)

    def assert_not_installed(self):
        self.assertEqual(g.source_config(self.source),self.config)
        self.assertEqual(SourceConfigVersion.query.count(),0)

    def assert_empty_update(self,previous):
        update=self.exploration(since=previous['structure_token'])
        self.assertEqual(update['update_mode'],'delta')
        self.assertEqual(update['structure_token'],previous['structure_token'])
        self.assertEqual(update['delta'],{field:dict(upsert=[],remove=[])for field in('nodes','relations')})

    def test_committed_scope_updates_choices_details_and_delta_without_installing(self):
        before=self.exploration()
        version=current_version(self.school.id)
        self.assertNotEqual(self.source_node(before).get('scope_kind'),'configured_module_preview')
        self.assertTrue(hold_configured_joint_scope(self.proposal))
        after=self.exploration()
        self.assertEqual(current_version(self.school.id)['revision'],version['revision']+1)
        self.assertEqual(after['structure_version'],current_version(self.school.id))
        node=self.source_node(after)
        self.assertEqual(node['scope_kind'],'configured_module_preview')
        self.assertEqual(node['source_scope']['selected_items'],3)
        self.assertEqual(node['verification'],'unknown')
        self.assertFalse(any(row['type']in('source_ownership','source_preview')and row['target']==node['id']
            for row in after['relations']))
        capture=read_structure(self.school)
        self.assertEqual(capture['version'],after['structure_version'])
        choice=next(column for groups in source_choices(capture).values()for group in groups
            for column in group['columns']if column['department'].id==self.source.id)
        self.assertEqual(choice['scope_kind'],node['scope_kind'])
        self.assertEqual(choice['source_scope']['selected_items'],3)
        update=self.exploration(since=before['structure_token'])
        self.assertEqual(update['update_mode'],'delta')
        rebuilt=self.fixture.apply_delta(before,update)
        for field in('nodes','relations'):
            self.assertEqual({row['id']:row for row in rebuilt[field]},{row['id']:row for row in after[field]})
            self.assertEqual(len(after[field]),len({row['id']for row in after[field]}))
        held_version=current_version(self.school.id)
        self.assertTrue(hold_configured_joint_scope(self.proposal))
        self.assertEqual(current_version(self.school.id),held_version)
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_preserved').count(),1)
        self.assert_empty_update(after)
        self.assert_not_installed()

    def test_failed_scope_commit_changes_neither_review_facts_nor_structure_version(self):
        before=self.exploration()
        version=current_version(self.school.id)
        state=self.proposal.state
        validation=self.proposal.validation_json
        def reject(session):raise ValueError('preview review transaction rejected')
        event.listen(Session,'before_commit',reject)
        try:
            with self.assertRaisesRegex(ValueError,'preview review transaction rejected'):
                hold_configured_joint_scope(self.proposal)
        finally:
            event.remove(Session,'before_commit',reject)
            db.session.rollback()
        db.session.refresh(self.proposal)
        self.assertEqual(self.proposal.state,state)
        self.assertEqual(self.proposal.validation_json,validation)
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_preserved').count(),0)
        self.assertEqual(current_version(self.school.id),version)
        self.assert_empty_update(before)
        self.assert_not_installed()

    def test_material_and_scope_publish_together_and_repeat_is_a_noop(self):
        from backend.database.models import CatalogPublication
        from backend.services.runtime_catalog import published_catalog
        from backend.services.source_inventory import site_key
        before=self.exploration();version=current_version(self.school.id)
        self.assertTrue(self.publish_scope())
        after=self.exploration()
        self.assertEqual(current_version(self.school.id)['revision'],version['revision']+1)
        self.assertEqual(self.source_node(after)['source_scope']['selected_items'],3)
        self.assertEqual(self.source_node(after)['verification'],'unknown')
        row=CatalogPublication.query.one();generation=row.generation
        self.assertEqual(published_catalog().snapshot(site_key(self.school.url),self.config['list_url']),preview_html())
        self.assertTrue(self.publish_scope())
        self.assertEqual(CatalogPublication.query.one().generation,generation)
        self.assertEqual(current_version(self.school.id),after['structure_version'])
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_preserved').count(),1)
        self.assert_empty_update(after)
        self.assert_not_installed()

    def test_rejected_material_publication_rolls_back_the_scope_and_pointer(self):
        from backend.database.models import CatalogPublication
        before=self.exploration();version=current_version(self.school.id)
        state,validation=self.proposal.state,self.proposal.validation_json
        def reject(session):raise ValueError('material and scope rejected together')
        event.listen(Session,'before_commit',reject)
        try:
            with self.assertRaisesRegex(ValueError,'material and scope rejected together'):
                self.publish_scope()
        finally:event.remove(Session,'before_commit',reject);db.session.rollback()
        db.session.refresh(self.proposal)
        self.assertEqual((self.proposal.state,self.proposal.validation_json),(state,validation))
        self.assertEqual(CatalogPublication.query.count(),0)
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_preserved').count(),0)
        self.assertEqual(current_version(self.school.id),version)
        self.assert_empty_update(before)
        self.assert_not_installed()

    def test_material_publication_refuses_changed_config_school_and_active_source(self):
        from backend.database.models import CatalogPublication
        name=self.source.name
        self.source.name=name+' changed';db.session.commit()
        with self.assertRaisesRegex(ValueError,'当前配置不匹配'):
            self.publish_scope()
        self.source.name=name;db.session.commit()
        school_name=self.school.name
        self.school.name=school_name+' changed';db.session.commit()
        with self.assertRaisesRegex(ValueError,'当前学校身份不匹配'):
            self.publish_scope()
        self.school.name=school_name;self.proposal.state='activated';db.session.commit()
        with self.assertRaisesRegex(ValueError,'尚未接入'):
            self.publish_scope()
        self.assertEqual(CatalogPublication.query.count(),0)
        self.assertEqual(SourceReviewEvent.query.filter_by(action='configured_scope_preserved').count(),0)
        self.assertEqual(SourceConfigVersion.query.count(),0)
