"""Legacy navigation page labels must not discard validated publisher originals."""
import json
import unittest
from backend.database.db import db
from backend.database.models import DepartmentDirectoryEntry
from backend.services import source_governance as governance
from backend.services.source_material_publication import _materials
from backend.services.runtime_catalog import pack
from tests import test_source_material_activation as fixture


class LegacyIdentityMaterialTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SourceMaterialActivationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def legacy_proposal(self,kind):
        initial=self.f.proposal();bundle=json.loads(initial.evidence_json)
        bundle.pop('publisher_evidence',None);bundle['publisher_material']={}
        for page in bundle['identity_pages']:
            if page['url']==fixture.fixture.CS:
                page['kind']=kind;page['label']='学生管理'
        proposal=governance.propose_source(self.f.f.school.id,self.f.config,governance.CapturedEvidence(bundle))
        self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
        return proposal,bundle

    def check_owner(self,kind):
        proposal,_=self.legacy_proposal(kind)
        source=governance.activate_proposal(proposal.id)
        owners=[fact for row in DepartmentDirectoryEntry.query.filter_by(department_id=source.id)
            for fact in row.facts_json if fact['type']=='source_ownership']
        self.assertTrue(owners,'Validation passed but its publisher original was omitted during publication')
        published=self.f.f.catalog.pin(self.f.f.key)
        self.assertEqual(published.snapshot(self.f.f.key,fixture.fixture.CS),self.f.f.pages[fixture.fixture.CS])

    def test_navigation_identity_material_keeps_verified_owner(self):
        self.check_owner('navigation')

    def test_group_identity_material_keeps_verified_owner(self):
        self.check_owner('group')

    def test_changed_parser_context_is_not_skipped_when_bytes_are_cached(self):
        _,bundle=self.legacy_proposal('navigation')
        self.f.f.catalog.publish(self.f.f.inventory,self.f.f.key)
        published=self.f.f.catalog.pin(self.f.f.key)
        page=published.get_page(self.f.f.key,fixture.fixture.CS)
        page['kind']='navigation';page['label']='学生管理'
        with published.connect(write=True)as connection:
            connection.execute('UPDATE catalog_pages SET page_gzip=? WHERE url=?',(pack(page),fixture.fixture.CS))
        materials=_materials(self.f.f.school,bundle,self.f.config,published)
        home=next((row for row in materials if row['url']==fixture.fixture.CS),None)
        self.assertIsNotNone(home,'A changed parsing context must be published even for identical bytes')
        self.assertEqual(home['kind'],'gateway')


if __name__=='__main__':unittest.main()
