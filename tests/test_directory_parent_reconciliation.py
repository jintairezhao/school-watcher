"""Metadata refresh must not flatten local roster cards or retain stale auto edges."""
import unittest
from copy import deepcopy
from unittest.mock import patch

import test_school_structure as fixture
from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry
from backend.services.directory_options import sync_directory_options
from backend.services.school_structure import sync_official_structure


class DirectoryParentReconciliationTests(unittest.TestCase):
    setUp = fixture.SchoolStructureTests.setUp
    tearDown = fixture.SchoolStructureTests.tearDown
    publish = fixture.SchoolStructureTests.publish

    def prepare_roster(self):
        self.pages[fixture.ROOT+'colleges/']=(
            '<main><h1>院系设置</h1><ul><li><h3>理工学部</h3><div><div><ul>'
            f'<li><a href="{fixture.CS}">计算机学院</a></li></ul></div></div></li></ul></main>')
        self.publish()
        self.parent=Department.query.filter_by(school_id=self.school.id,name='院系设置',kind='group').one()
        self.child=Department.query.filter_by(school_id=self.school.id,name='计算机学院',kind='unit').one()
        self.node=next(n for n in self.catalog.structure(self.key)
            if n['name']=='计算机学院' and n['reference_url']==fixture.ROOT+'colleges/' and n['relation']=='directory_entry')

    def add_old_flat_copy(self, manual=False):
        n=self.node
        auto={'writer':'directory_options','type':'directory_listing','observed_relation':'directory_entry',
              'node_key':n['node_key'],'evidence':[{k:n[k] for k in ('reference_url','locator','content_hash','observed_at')}]}
        self.manual={'writer':'manual','type':'cross_entity_reference','note':'preserve explicit user data'}
        row=DepartmentDirectoryEntry(parent_id=self.parent.id,department_id=self.child.id,position=0,
                                     facts_json=[auto]+([self.manual] if manual else []))
        db.session.add(row);db.session.commit()
        return row

    def test_metadata_refresh_does_not_flatten_a_card_member_under_the_page(self):
        self.prepare_roster()
        sync_directory_options(self.school,self.catalog)
        self.assertIsNone(DepartmentDirectoryEntry.query.filter_by(
            parent_id=self.parent.id,department_id=self.child.id).first())
        cards=Department.query.filter_by(school_id=self.school.id,name='理工学部').all()
        self.assertTrue(any(DepartmentDirectoryEntry.query.filter_by(
            parent_id=card.id,department_id=self.child.id).first() for card in cards))

    def test_same_hash_parent_correction_retires_only_the_automatic_copy(self):
        self.prepare_roster();self.add_old_flat_copy(manual=True)
        ids=[d.id for d in self.school.departments.order_by(Department.id)]
        sync_official_structure(self.school,self.catalog)
        row=DepartmentDirectoryEntry.query.filter_by(parent_id=self.parent.id,department_id=self.child.id).one()
        self.assertEqual(row.facts_json,[self.manual])
        self.assertEqual(ids,[d.id for d in self.school.departments.order_by(Department.id)])

    def test_an_unavailable_replacement_cannot_retire_the_old_auto_copy(self):
        self.prepare_roster();old=self.add_old_flat_copy();before=deepcopy(old.facts_json)
        report=self.catalog.report(self.key)
        for page in report['pages']:
            if page['url']==self.node['reference_url']:page['state']='pending'
        with patch.object(self.catalog,'report',return_value=report):
            sync_official_structure(self.school,self.catalog)
        self.assertEqual(db.session.get(DepartmentDirectoryEntry,(self.parent.id,self.child.id)).facts_json,before)


if __name__=='__main__':unittest.main()
