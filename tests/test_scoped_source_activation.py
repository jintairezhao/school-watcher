"""A source operation owns its placements and necessary units, not the whole school."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
import json
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Department,DepartmentDirectoryEntry
from backend.services import source_governance as g
from backend.services.school_structure import FACT_WRITER
from backend.services.structure_versions import current_version
from backend.services.structure_snapshot import read_structure,source_choices,source_paths,graph_projection,transport_update
from backend.scraper.discovery.inventory_crawler import inspect_page
from tests import test_school_structure as fixture
from tests import test_direct_onboarding as native


class ScopedSourceActivationTests(unittest.TestCase):
    setUp=fixture.SchoolStructureTests.setUp
    tearDown=fixture.SchoolStructureTests.tearDown
    publish=fixture.SchoolStructureTests.publish
    connect=fixture.SchoolStructureTests.connect

    def prepared(self):
        self.publish()
        target=self.connect(fixture.CS+'notices/')['department_ids'][0]
        other=self.connect(fixture.LANG+'notices/')['department_ids'][0]
        return target,other

    def mark_other(self,other):
        link=DepartmentDirectoryEntry.query.filter_by(department_id=other).first()
        link.facts_json=[*deepcopy(link.facts_json),dict(writer=FACT_WRITER,type='directory_listing',
            observed_relation='retained_context_for_another_source',evidence=[])]
        db.session.commit()
        return link,deepcopy(link.facts_json)

    def proposal(self,url,group,ident=None):
        config=(g.source_config(db.session.get(Department,ident)) if ident else
                {f:native.ai_column().get(f,'') for f in g.FIELDS})
        config.update(list_url=url,group_name=group)
        evidence=g.capture_source_evidence(self.school.id,config,department_id=ident,inventory=self.inventory,
            fetcher=lambda address,purpose:self.fixture.fetch(address,purpose) if purpose=='body' else fixture.HTML)
        proposal=g.propose_source(self.school.id,config,evidence,department_id=ident)
        self.assertTrue(g.validate_proposal(proposal.id)['passed'])
        return proposal

    def assert_views(self,ident,group):
        capture=read_structure(self.school)
        graph=graph_projection(capture,self.school)
        config=g.source_config(db.session.get(Department,ident))
        node=next(n for n in graph['nodes'] if n['id']=='source:'+str(ident))
        self.assertEqual((node['label'],node['url'],node['verification']),
                         (config['name'],config['list_url'],'verified'))
        choices=source_choices(capture)
        owners=[unit['name'] for units in choices.values() for unit in units
                if any(c['department'].id==ident for c in unit['own_columns'])]
        self.assertTrue(owners)
        self.assertEqual(set(owners),{group})
        self.assertTrue(any(p['relation_type']=='source_ownership' for p in source_paths(capture).get(ident,[])))
        self.assertEqual(graph['structure_version'],capture['version'])
        return graph

    def test_validated_edit_preserves_other_sources_and_organization_rows(self):
        target,other=self.prepared()
        proposal=self.proposal(fixture.CS+'notices/','计算机学院',target)
        link,before=self.mark_other(other)
        units={r.id:deepcopy(r.identity_json) for r in Department.query.filter(Department.kind!='column')}
        old=transport_update(graph_projection(read_structure(self.school),self.school),school_id=self.school.id)
        g.activate_proposal(proposal.id)
        self.assertEqual(db.session.get(DepartmentDirectoryEntry,(link.parent_id,other)).facts_json,before)
        self.assertEqual({r.id:r.identity_json for r in Department.query.filter(Department.kind!='column')},units)
        graph=self.assert_views(target,'计算机学院')
        delta=transport_update(graph,school_id=self.school.id,since=old['structure_token'])
        self.assertEqual(delta['update_mode'],'delta')
        for field in ('nodes','relations'):
            merged={row['id']:row for row in old[field]}
            for ident in delta['delta'][field]['remove']:merged.pop(ident,None)
            merged.update({row['id']:row for row in delta['delta'][field]['upsert']})
            self.assertEqual(merged,{row['id']:row for row in graph[field]})

    def test_native_source_refresh_preserves_other_sources(self):
        from backend.services.source_onboarding import _install
        from backend.database.source_governance_models import SourceValidation
        target,other=self.prepared()
        link,before=self.mark_other(other)
        config=g.source_config(db.session.get(Department,target))
        validation=json.loads(SourceValidation.query.filter_by(department_id=target).order_by(SourceValidation.id.desc()).first().validation_json)
        records,errors=g._extract(config,validation['list_snapshot'])[2:4]
        self.assertFalse(errors)
        result,_=_install(self.school.id,config,records,repair=True,repair_department_id=target,
                          previous_url=config['list_url'],validation=validation)
        self.assertEqual(result,target)
        self.assertEqual(db.session.get(DepartmentDirectoryEntry,(link.parent_id,other)).facts_json,before)
        self.assert_views(target,'计算机学院')

    def test_repeat_activation_does_not_reconcile_unrelated_facts_or_increment_version(self):
        target,other=self.prepared()
        proposal=self.proposal(fixture.CS+'notices/','计算机学院',target)
        g.activate_proposal(proposal.id)
        link,before=self.mark_other(other)
        version=current_version(self.school.id)
        g.activate_proposal(proposal.id)
        self.assertEqual(db.session.get(DepartmentDirectoryEntry,(link.parent_id,other)).facts_json,before)
        self.assertEqual(current_version(self.school.id),version)

    def test_new_source_materializes_its_real_unit_without_creating_other_new_units(self):
        target,other=self.prepared()
        new='https://med.example.edu.cn/'
        unrelated='https://art.example.edu.cn/'
        self.pages[fixture.ROOT+'colleges/']+=f'<ul><li><a href="{new}">医学院</a></li><li><a href="{unrelated}">艺术学院</a></li></ul>'
        for url,kind,label,html in [
            (fixture.ROOT+'colleges/','directory','院系设置',self.pages[fixture.ROOT+'colleges/']),
            (new,'unit','医学院','<title>测试大学医学院</title><nav><a href="/notices/">通知公告</a></nav>'),
            (unrelated,'unit','艺术学院','<title>测试大学艺术学院</title>'),
            (new+'notices/','channel','通知公告',fixture.HTML),
        ]:
            self.inventory.enqueue(self.key,url,label,kind,1,[],'school_domain')
            inspect_page(self.inventory,self.inventory.report(self.key)['site'],self.inventory.get_page(self.key,url),
                fetcher=lambda address,body=html:dict(url=address,status=200,html=body,
                    checked_at=datetime.now(timezone.utc).isoformat()))
        # The source writer will publish only its captured materials. An explicit
        # school-wide catalogue publication would intentionally reconcile every
        # roster entry and therefore is a different operation.
        proposal=self.proposal(new+'notices/','医学院')
        g.activate_proposal(proposal.id)
        self.assertEqual({r.name for r in Department.query.filter_by(kind='unit')},
                         {'计算机学院','外国语学院','医学院'})
        unit=Department.query.filter_by(kind='unit',name='医学院').one()
        self.assertTrue(DepartmentDirectoryEntry.query.filter_by(parent_id=unit.id,department_id=proposal.department_id).count())
        self.assert_views(proposal.department_id,'医学院')
        self.assert_views(target,'计算机学院')
        self.assert_views(other,'外国语学院')

    def test_failure_after_scoped_fact_changes_rolls_back_the_whole_save(self):
        from backend.services.school_structure import sync_source_structure
        from backend.database.source_governance_models import SourceConfigVersion
        target,other=self.prepared()
        proposal=self.proposal(fixture.CS+'notices/','计算机学院',target)
        link,before=self.mark_other(other)
        config=g.source_config(db.session.get(Department,target))
        versions=SourceConfigVersion.query.count()
        version=current_version(self.school.id)
        def fail_after_changes(source,catalog,*,commit=True):
            sync_source_structure(source,catalog,commit=False)
            raise RuntimeError('injected after scoped facts')
        with patch('backend.services.school_structure.sync_source_structure',side_effect=fail_after_changes):
            with self.assertRaisesRegex(RuntimeError,'injected after scoped facts'):
                g.activate_proposal(proposal.id)
        self.assertEqual(g.source_config(db.session.get(Department,target)),config)
        self.assertEqual(db.session.get(DepartmentDirectoryEntry,(link.parent_id,other)).facts_json,before)
        self.assertEqual(SourceConfigVersion.query.count(),versions)
        self.assertEqual(current_version(self.school.id),version)
        self.assertEqual(proposal.state,'validated')
