"""Governance cleanup retains raw material referenced only by a published catalogue."""
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import CatalogPublication, Department, Subscription
from backend.database.source_governance_models import SourceProposal, SourceValidation
from backend.services import source_governance as governance
from backend.services.runtime_catalog import RuntimeCatalog, pack
from backend.services.structure_snapshot import read_structure, graph_projection
from backend.services.structure_versions import current_version
from tests import test_school_structure as fixture


class GovernanceCatalogRetentionTests(unittest.TestCase):
    def setUp(self):
        self.guards=[]
        for target in ('socket.socket.connect','socket.create_connection','socket.getaddrinfo','backend.ai.providers.complete'):
            guard=patch(target,side_effect=AssertionError('No external access in evidence cleanup tests'))
            guard.start();self.addCleanup(guard.stop);self.guards.append(guard)
        self.f=fixture.SchoolStructureTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.publish()
        self.root=governance._evidence_root()

    def age(self,path):
        old=time.time()-40*86400
        os.utime(path,(old,old))
        return path

    def retained_home(self):
        ref=governance._snapshot(self.f.pages[fixture.ROOT],fixture.ROOT,role='identity')
        self.assertEqual(ref['hash'],self.f.catalog.get_page(self.f.key,fixture.ROOT)['content_hash'])
        self.age(self.root/(ref['hash']+'.html.gz'))
        self.assertEqual(SourceProposal.query.count(),0)
        self.assertEqual(SourceValidation.query.count(),0)
        return ref

    def orphan(self,text='<p>expired unreferenced bytes</p>'):
        ref=governance._snapshot(text,fixture.ROOT+'unused/')
        return self.age(self.root/(ref['hash']+'.html.gz'))

    def before_cleanup(self):
        capture=read_structure(self.f.school)
        return dict(version=current_version(self.f.school.id),
            graph=deepcopy(graph_projection(capture,self.f.school)),
            sources=[row.to_dict() for row in Department.query.order_by(Department.id)],
            subscriptions=[(row.id,row.department_ids) for row in Subscription.query.order_by(Subscription.id)])

    def assert_unchanged(self,before):
        self.assertEqual(self.before_cleanup(),before)

    def test_manual_cleanup_preserves_catalogue_only_homepage(self):
        ref=self.retained_home();orphan=self.orphan();before=self.before_cleanup()
        result=governance.prune_governance_evidence(7,all_cache=True)
        self.assertTrue((self.root/(ref['hash']+'.html.gz')).exists())
        self.assertEqual(governance.read_snapshot(ref),self.f.pages[fixture.ROOT])
        self.assertFalse(orphan.exists())
        self.assertEqual(result['removed'],1)
        self.assert_unchanged(before)

    def test_scheduled_cleanup_preserves_catalogue_only_homepage(self):
        ref=self.retained_home();orphan=self.orphan()
        result=governance.prune_governance_evidence(7)
        self.assertTrue((self.root/(ref['hash']+'.html.gz')).exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(result['removed'],1)

    def set_snapshot_note(self,reference):
        published=self.f.catalog.pin(self.f.key)
        page=published.get_page(self.f.key,fixture.ROOT)
        notes=json.loads(page.get('notes_json')or'[]')
        page['notes_json']=json.dumps([*notes,'full_snapshot:'+json.dumps(reference)])
        # Deliberately add retained-reference metadata in this isolated fixture.
        with published.connect(write=True) as connection:
            connection.execute('UPDATE catalog_pages SET page_gzip=? WHERE url=?',(pack(page),fixture.ROOT))

    def test_current_snapshot_preserves_referenced_script_and_pdf(self):
        ref=self.retained_home()
        script=governance._snapshot('const noticeTab = 1;',fixture.ROOT+'tab.js',role='script')
        script_path=self.age(self.root/(script['hash']+'.html.gz'))
        payload=b'%PDF-1.4\n%%EOF\n'
        digest=hashlib.sha256(payload).hexdigest()
        pdf=self.root/(digest+'.pdf');pdf.write_bytes(payload);self.age(pdf)
        self.set_snapshot_note(dict(ref,script_materials=[script],embedded_pdf={'sha256':digest}))
        orphan=self.orphan()
        result=governance.prune_governance_evidence(all_cache=True)
        self.assertTrue(script_path.exists());self.assertTrue(pdf.exists())
        self.assertTrue((self.root/(ref['hash']+'.html.gz')).exists())
        self.assertFalse(orphan.exists());self.assertEqual(result['removed'],1)

    def test_stale_snapshot_note_does_not_pin_unreferenced_material(self):
        current=self.retained_home()
        stale=self.orphan('<p>old page version</p>')
        self.set_snapshot_note({'hash':stale.name.removesuffix('.html.gz')})
        governance.prune_governance_evidence(all_cache=True)
        self.assertFalse(stale.exists())
        self.assertTrue((self.root/(current['hash']+'.html.gz')).exists())

    def test_retained_history_protects_raw_copy_until_generation_is_removed(self):
        ref=self.retained_home();old=self.f.catalog.pin(self.f.key)
        new_html='<title>测试大学</title><nav><a href="/colleges/">院系设置（更新）</a></nav>'
        from backend.scraper.discovery.inventory_crawler import inspect_page
        inspect_page(self.f.inventory,self.f.inventory.report(self.f.key)['site'],
            self.f.inventory.get_page(self.f.key,fixture.ROOT),
            fetcher=lambda url:dict(url=url,status=200,html=new_html),ai_assist=False)
        self.f.catalog.publish(self.f.inventory,self.f.key,merge=True)
        self.assertNotEqual(self.f.catalog.pin(self.f.key).path,old.path)
        self.assertNotEqual(self.f.catalog.get_page(self.f.key,fixture.ROOT)['content_hash'],ref['hash'])
        path=self.root/(ref['hash']+'.html.gz')
        governance.prune_governance_evidence(all_cache=True)
        self.assertTrue(path.exists())
        # Only this no-longer-active test generation is removed, emulating retirement.
        old.path.unlink()
        governance.prune_governance_evidence(all_cache=True)
        self.assertFalse(path.exists())

    def test_legacy_catalogue_without_publication_pointer_is_protected(self):
        ref=self.retained_home()
        path=Path(self.f.fixture.temp.name)/'legacy.db'
        legacy=RuntimeCatalog(path,versioned=False)
        legacy.publish(self.f.inventory,self.f.key)
        CatalogPublication.query.delete();db.session.commit()
        with patch.dict(self.f.fixture.app.config,SOURCE_CATALOG_PATH=str(path)):
            governance.prune_governance_evidence(all_cache=True)
        self.assertTrue((self.root/(ref['hash']+'.html.gz')).exists())

    def test_missing_active_generation_blocks_cleanup_before_any_deletion(self):
        self.retained_home();orphan=self.orphan()
        published=self.f.catalog.pin(self.f.key);published.path.unlink()
        with self.assertRaisesRegex(RuntimeError,'目录版本缺失'):
            governance.prune_governance_evidence(all_cache=True)
        self.assertTrue(orphan.exists())

    def test_publication_lock_is_still_held_when_orphan_is_deleted(self):
        from filelock import FileLock,Timeout
        self.retained_home();orphan=self.orphan();deleted=[]
        original=Path.unlink
        def observed(path,*args,**kwargs):
            if path==orphan:
                other=FileLock(str(self.f.catalog.path.resolve())+'.publish.lock',timeout=0)
                with self.assertRaises(Timeout):
                    with other:
                        pass
                deleted.append(True)
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',observed):
            governance.prune_governance_evidence(all_cache=True)
        self.assertEqual(deleted,[True])


if __name__=='__main__':unittest.main()
