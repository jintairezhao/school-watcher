"""Cache cleanup must not retract a still-evidenced published relationship."""
from copy import deepcopy
import gzip
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import CatalogPublication,Department,DepartmentDirectoryEntry,Subscription
from backend.services.discovery_cache import DiscoveryCache
from backend.services.runtime_catalog import RuntimeCatalog
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.structure_snapshot import read_structure,graph_projection
from backend.services.structure_versions import current_version
from tests import test_school_structure as fixture


class StructureMaterialRetentionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchoolStructureTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.model=patch('backend.ai.providers.complete',side_effect=AssertionError('No model call'))
        self.model.start();self.addCleanup(self.model.stop)
        self.address=fixture.ROOT+'colleges/'
        self.f.pages[self.address]=('<h1>院系设置</h1><table><tr><th>学院</th><th>下属单位</th></tr>'
            '<tr><td>科学学院</td><td><a href="'+fixture.CS+'">计算机学院</a></td></tr></table>')
        self.f.publish()
        self.cache=DiscoveryCache(self.f.inventory.path)

    def facts(self):
        return deepcopy([(row.parent_id,row.department_id,row.facts_json)
            for row in DepartmentDirectoryEntry.query.order_by(DepartmentDirectoryEntry.parent_id,DepartmentDirectoryEntry.department_id)])

    def test_cleared_html_and_republication_preserve_current_organization_proof(self):
        before=self.facts();version=current_version(self.f.school.id)
        pinned=self.f.catalog.pin(self.f.key);material=pinned.snapshot(self.f.key,self.address)
        self.assertEqual(material,self.f.pages[self.address])
        graph=graph_projection(read_structure(self.f.school),self.f.school,scope='exploration')
        self.assertTrue(any(edge['type']=='organization_membership' for edge in graph['relations']))
        subscriptions=[(row.id,row.department_ids) for row in Subscription.query.all()]
        self.cache.trim(all_cache=True)
        self.assertIsNone(self.cache.snapshot(self.f.key,self.address))
        self.f.catalog.publish(self.cache,self.f.key)
        self.assertEqual(self.f.catalog.snapshot(self.f.key,self.address),material)
        self.assertEqual(self.facts(),before)
        self.assertEqual(current_version(self.f.school.id),version)
        self.assertEqual([(row.id,row.department_ids) for row in Subscription.query.all()],subscriptions)
        self.assertEqual(pinned.snapshot(self.f.key,self.address),material)

    def inspect(self,html):
        inspect_page(self.cache,self.cache.report(self.f.key)['site'],self.cache.get_page(self.f.key,self.address),
            fetcher=lambda url:dict(url=url,status=200,html=html),ai_assist=False)

    def test_merge_republication_also_preserves_exact_current_material(self):
        before=self.facts();material=self.f.pages[self.address]
        self.cache.trim(all_cache=True)
        self.f.catalog.publish(self.cache,self.f.key,merge=True)
        self.assertEqual(self.f.catalog.snapshot(self.f.key,self.address),material)
        self.assertEqual(self.facts(),before)

    def test_source_owner_and_subscription_identity_survive_cleanup(self):
        result=self.f.connect(fixture.CS+'notices/');ident=result['department_ids'][0]
        before=self.facts()
        self.assertTrue(any(fact['type']=='source_ownership' for row in before if row[1]==ident for fact in row[2]))
        selector=db.session.get(Department,ident).list_selector
        version=current_version(self.f.school.id)
        self.cache.trim(all_cache=True)
        self.f.catalog.publish(self.cache,self.f.key)
        self.assertEqual(self.facts(),before)
        self.assertEqual(current_version(self.f.school.id),version)
        self.assertEqual(db.session.get(Department,ident).list_selector,selector)
        graph=graph_projection(read_structure(self.f.school),self.f.school)
        self.assertTrue(any(edge['type']=='source_ownership' and edge['target']==f'source:{ident}' for edge in graph['relations']))

    def test_new_material_does_not_inherit_old_bytes_when_merge_keeps_the_page(self):
        old=self.f.catalog.pin(self.f.key)
        html='<main><h1>院系设置</h1><p>目录更新中</p></main>'
        self.inspect(html)
        self.f.catalog.publish(self.cache,self.f.key,merge=True)
        self.assertNotEqual(self.f.catalog.get_page(self.f.key,self.address)['content_hash'],old.get_page(self.f.key,self.address)['content_hash'])
        self.assertEqual(self.f.catalog.snapshot(self.f.key,self.address),'')
        self.assertEqual(old.snapshot(self.f.key,self.address),self.f.pages[self.address])
        self.assertFalse(any(fact['type']=='organization_membership' for row in self.facts() for fact in row[2]))

    def test_changed_material_with_evicted_body_cannot_borrow_an_older_proof(self):
        self.inspect(self.f.pages[self.address]+'<p>Changed material version</p>')
        self.cache.trim(all_cache=True)
        self.f.catalog.publish(self.cache,self.f.key)
        self.assertEqual(self.f.catalog.snapshot(self.f.key,self.address),'')
        self.assertFalse(any(fact['type']=='organization_membership' for row in self.facts() for fact in row[2]))

    def test_inconsistent_cached_body_blocks_activation_without_changing_memberships(self):
        before=self.facts();version=current_version(self.f.school.id)
        pinned=self.f.catalog.pin(self.f.key);real=self.cache.snapshot
        def wrong(key,url):return '<p>Wrong cached bytes</p>' if url==self.address else real(key,url)
        with patch.object(self.cache,'snapshot',side_effect=wrong):
            with self.assertRaisesRegex(ValueError,'证据.*指纹'):
                self.f.catalog.publish(self.cache,self.f.key)
        self.assertEqual(self.facts(),before)
        self.assertEqual(current_version(self.f.school.id),version)
        self.assertEqual(self.f.catalog.pin(self.f.key).path,pinned.path)

    def test_corrupt_prior_material_is_not_republished_as_verified(self):
        pinned=self.f.catalog.pin(self.f.key);before=self.facts();version=current_version(self.f.school.id)
        # Deliberate damage to an isolated immutable-generation fixture.
        with pinned.connect(write=True) as connection:
            connection.execute('UPDATE catalog_pages SET evidence_gzip=? WHERE url=?',
                (gzip.compress(b'<p>Corrupted retained proof</p>'),self.address))
        self.cache.trim(all_cache=True)
        with self.assertRaisesRegex(ValueError,'证据.*指纹'):
            self.f.catalog.publish(self.cache,self.f.key)
        self.assertEqual(self.facts(),before)
        self.assertEqual(current_version(self.f.school.id),version)
        self.assertEqual(self.f.catalog.pin(self.f.key).path,pinned.path)

    def test_fresh_verified_material_can_replace_a_damaged_previous_copy(self):
        pinned=self.f.catalog.pin(self.f.key)
        with pinned.connect(write=True) as connection:
            connection.execute('UPDATE catalog_pages SET evidence_gzip=? WHERE url=?',
                (b'broken compressed data',self.address))
        self.f.catalog.publish(self.cache,self.f.key)
        self.assertEqual(self.f.catalog.snapshot(self.f.key,self.address),self.f.pages[self.address])

    def test_reuse_does_not_cross_a_site_url_or_material_version(self):
        pinned=self.f.catalog.pin(self.f.key)
        digest=hashlib.sha256(self.f.pages[self.address].encode()).hexdigest()
        self.assertEqual(pinned._matching_materials('another-site',{self.address:digest}),{})
        self.assertEqual(pinned._matching_materials(self.f.key,{self.address+'other':digest}),{})
        self.assertEqual(pinned._matching_materials(self.f.key,{self.address:'f'*64}),{})

    def test_mutable_legacy_catalogue_reads_prior_bytes_before_replacing_rows(self):
        catalog=RuntimeCatalog(Path(self.f.fixture.temp.name)/'mutable.db',versioned=False)
        catalog.publish(self.cache,self.f.key)
        self.cache.trim(all_cache=True)
        for merge in (False,True):
            catalog.publish(self.cache,self.f.key,merge=merge)
            self.assertEqual(catalog.snapshot(self.f.key,self.address),self.f.pages[self.address])

    def test_cleaned_current_evidence_survives_full_backup_and_restoration(self):
        from backend import create_app
        from backend.database.models import School
        from backend.services.backups import create_backup,restore_backup
        result=self.f.connect(fixture.CS+'notices/')
        source_id=result['department_ids'][0];school_id=self.f.school.id
        self.cache.trim(all_cache=True);self.f.catalog.publish(self.cache,self.f.key)
        before=graph_projection(read_structure(self.f.school),self.f.school,scope='exploration')
        root=Path(self.f.fixture.temp.name)
        with patch.dict(self.f.fixture.app.config,dict(BACKUP_DIR=str(root/'backups'),BACKUP_COPY_DIR='')):
            saved=create_backup()
        destination=root/'restored'
        restore_backup(root/'backups'/saved['backup'],destination)
        restored=create_app(dict(TESTING=True,SECRET_KEY='restored-material-fixture',
            SQLALCHEMY_DATABASE_URI='sqlite:///'+str(destination/Path(db.engine.url.database).name),
            SOURCE_CATALOG_PATH=str(destination/self.f.catalog.path.name),
            DISCOVERY_CACHE_PATH=str(destination/'must-not-open-scratch.db'),
            SOURCE_GOVERNANCE_EVIDENCE_PATH=str(destination/'source-governance-evidence')))
        with restored.app_context(),patch('backend.services.source_inventory.Inventory.__init__',side_effect=AssertionError('No scratch read')):
            try:
                school=db.session.get(School,school_id)
                after=graph_projection(read_structure(school),school,scope='exploration')
                self.assertEqual({n['id'] for n in before['nodes']},{n['id'] for n in after['nodes']})
                self.assertEqual({e['id'] for e in before['relations']},{e['id'] for e in after['relations']})
                self.assertEqual(after['source_count'],1)
                self.assertEqual(RuntimeCatalog(restored.config['SOURCE_CATALOG_PATH']).snapshot(self.f.key,self.address),self.f.pages[self.address])
                self.assertEqual(RuntimeCatalog(restored.config['SOURCE_CATALOG_PATH']).snapshot(self.f.key,fixture.ROOT),self.f.pages[fixture.ROOT])
                self.assertEqual(db.session.get(Department,source_id).list_url,fixture.CS+'notices/')
                self.assertNotEqual(before['structure_version']['epoch'],after['structure_version']['epoch'])
            finally:
                db.session.remove();db.engine.dispose()

    def test_actual_snapshot_api_keeps_relationships_and_version_after_cache_eviction(self):
        from backend.database.models import User
        client=self.f.fixture.app.test_client()
        with client.session_transaction() as session:session.update(user_id=User.query.one().id,_csrf_token='isolated-proof')
        path=f'/api/schools/{self.f.school.id}/structure-snapshot?scope=exploration'
        response=client.get(path);self.assertEqual(response.status_code,200)
        before=response.json
        self.assertTrue(any(edge['type']=='organization_membership' for edge in before['relations']))
        self.cache.trim(all_cache=True);self.f.catalog.publish(self.cache,self.f.key)
        response=client.get(path);self.assertEqual(response.status_code,200)
        after=response.json
        self.assertEqual(before['structure_version'],after['structure_version'])
        self.assertEqual(before['nodes'],after['nodes'])
        self.assertEqual(before['relations'],after['relations'])

    def test_navigation_without_an_owner_or_review_keeps_its_source_body(self):
        root=fixture.ROOT
        self.assertNotIn(root,{row.get('reference_url') for row in self.f.catalog.report(self.f.key).get('reference_checks',[])})
        self.assertEqual(self.f.catalog.snapshot(self.f.key,root),self.f.pages[root])

    def test_navigation_material_survives_cache_cleanup_and_republication(self):
        root=fixture.ROOT;body=self.f.pages[root]
        self.assertEqual(self.f.catalog.snapshot(self.f.key,root),body)
        version=current_version(self.f.school.id)
        self.cache.trim(all_cache=True)
        self.assertIsNone(self.cache.snapshot(self.f.key,root))
        self.f.catalog.publish(self.cache,self.f.key,merge=True)
        self.assertEqual(self.f.catalog.snapshot(self.f.key,root),body)
        self.assertEqual(current_version(self.f.school.id),version)

    def test_changed_navigation_with_missing_body_does_not_reuse_old_bytes(self):
        root=fixture.ROOT;previous=self.f.catalog.pin(self.f.key)
        body='<title>测试大学</title><nav><a href="/new-directory/">院系设置</a></nav>'
        inspect_page(self.cache,self.cache.report(self.f.key)['site'],self.cache.get_page(self.f.key,root),
            fetcher=lambda url:dict(url=url,status=200,html=body),ai_assist=False)
        self.cache.trim(all_cache=True)
        self.f.catalog.publish(self.cache,self.f.key,merge=True)
        self.assertEqual(self.f.catalog.snapshot(self.f.key,root),'')
        self.assertEqual(previous.snapshot(self.f.key,root),self.f.pages[root])

    def test_corrupt_fresh_navigation_body_blocks_the_whole_activation(self):
        root=fixture.ROOT;pinned=self.f.catalog.pin(self.f.key)
        version=current_version(self.f.school.id);facts=self.facts();actual=self.cache.snapshot
        def corrupted(key,url):return '<p>Wrong navigation material</p>' if url==root else actual(key,url)
        with patch.object(self.cache,'snapshot',side_effect=corrupted):
            with self.assertRaisesRegex(ValueError,'证据.*指纹'):
                self.f.catalog.publish(self.cache,self.f.key,merge=True)
        self.assertEqual(self.f.catalog.pin(self.f.key).path,pinned.path)
        self.assertEqual(current_version(self.f.school.id),version)
        self.assertEqual(self.facts(),facts)


if __name__=='__main__':unittest.main()
