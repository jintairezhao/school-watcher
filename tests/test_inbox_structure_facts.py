"""Inbox uses the same captured facts and audience policy as source settings."""
from copy import deepcopy
from datetime import datetime
from unittest.mock import patch
import unittest

from bs4 import BeautifulSoup
from flask import template_rendered
from backend.database.db import db
from backend.database.models import Announcement,Department,DepartmentDirectoryEntry,Subscription
from backend.services.structure_snapshot import read_structure,source_choices
from backend.services.structure_versions import current_version
from sqlalchemy.orm import Session
from tests import test_structure_snapshot as fixture
from tests.test_source_attention_flow import POLICY


class InboxStructureFactTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.StructureSnapshotTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.school=self.f.school;self.client=self.f.client
        self.user_id=self.f.fixture.fixture.admin.id
        db.session.add(Subscription(user_id=self.user_id,school_id=self.school.id));db.session.commit()
        self.platform=self.f.source('平台更新',self.school.url+'/platform/')
        self.teacher=self.f.source('教师发展',self.school.url+'/teacher/')
        self.policy=patch('backend.services.discovery_policy.get_attention_policy',return_value=POLICY)
        self.policy.start();self.addCleanup(self.policy.stop)
        self.path=f'/?school={self.school.id}&period=all'

    def render(self,path=None):
        captured=[]
        def receive(sender,template,context,**kwargs):captured.append(context)
        with template_rendered.connected_to(receive,self.f.app):
            response=self.client.get(path or self.path)
        self.assertEqual(response.status_code,200,response.text[:500])
        return BeautifulSoup(response.text,'html.parser'),captured[-1]

    def test_sidebar_uses_only_captured_directory_context(self):
        with patch('backend.services.source_placements.official_source_placements',side_effect=AssertionError('No fresh catalogue lookup')), \
                patch('backend.services.source_channels.legacy_channels_for',side_effect=AssertionError('No uncaptured publisher lookup')):
            soup,context=self.render()
        panel=soup.select_one('#sourcePanel')
        self.assertEqual(panel['data-attention-policy-version'],context['source_attention_policy_version'])
        self.assertEqual(int(panel['data-structure-revision']),current_version(self.school.id)['revision'])
        choices=source_choices(read_structure(self.school))
        expected=[c['department'].id for units in choices.values() for unit in units for c in unit['own_columns']]
        visible=[int(i['value']) for i in panel.select('[data-department]') if int(i['value']) in expected]
        self.assertEqual(visible,expected)
        self.assertEqual(expected,[self.teacher.id,self.platform.id])

    def test_ordinary_directory_membership_does_not_become_a_publisher_in_sidebar(self):
        owner=Department(school_id=self.school.id,name='Legacy guessed owner',kind='unit',list_url=self.school.url+'/owner/')
        db.session.add(owner);db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=owner.id,department_id=self.teacher.id));db.session.commit()
        soup,_=self.render()
        unit=soup.select_one('#sourcePanel [data-source-unit]')
        self.assertIsNotNone(unit)
        self.assertEqual(unit['data-source-unit-basis'],'display_directory')
        self.assertIn('记录分组',unit.get_text())
        self.assertFalse(any(edge['type']=='source_ownership' and edge['target']==f'source:{self.teacher.id}'
            for edge in read_structure(self.school)['facts']['relations']))

    def test_historical_unconfigured_source_stays_available_without_faking_connection(self):
        historical=Department.query.filter_by(school_id=self.school.id,list_selector=None).first()
        self.assertGreater(Announcement.query.filter_by(department_id=historical.id).count(),0)
        soup,_=self.render()
        choice=soup.select_one(f'#sourcePanel [data-department][value="{historical.id}"]')
        self.assertIsNotNone(choice)
        self.assertFalse(choice.has_attr('disabled'))
        self.assertFalse(historical.list_selector)

    def test_captured_configuration_does_not_refresh_after_later_commit(self):
        capture=read_structure(self.school);before=deepcopy(capture['version'])
        old_name=self.teacher.name;old_selector=self.teacher.list_selector
        self.teacher.name='New title after capture';self.teacher.list_selector='section.changed li';db.session.commit()
        columns=[c for units in source_choices(capture).values() for unit in units for c in unit['own_columns']]
        column=next(c for c in columns if c['department'].id==self.teacher.id)
        self.assertEqual(column['department'].name,old_name)
        self.assertEqual(column['department'].list_selector,old_selector)
        self.assertEqual(capture['version'],before)

    def test_modern_group_filter_and_refresh_do_not_widen_subscription(self):
        from backend.services.inbox_refresh import resolve_scope
        sub=Subscription.query.filter_by(user_id=self.user_id,school_id=self.school.id).one()
        sub.department_ids=[self.teacher.id]
        for row,title in ((self.teacher,'Allowed teacher notice'),(self.platform,'Unsubscribed platform notice')):
            db.session.add(Announcement(school_id=self.school.id,department_id=row.id,title=title,
                url=row.list_url+'notice',published_at=datetime.utcnow()))
        db.session.commit()
        soup,_=self.render(self.path+'&group=栏目（归属待核实）')
        self.assertIn('Allowed teacher notice',soup.get_text())
        self.assertNotIn('Unsubscribed platform notice',soup.get_text())
        sources=resolve_scope(self.user_id,dict(school_id=self.school.id,group='栏目（归属待核实）'))
        self.assertEqual([row.id for row in sources],[self.teacher.id])

    def test_configured_source_leaf_is_not_expanded_as_a_directory_filter(self):
        from backend.services.inbox_refresh import resolve_scope
        db.session.add(DepartmentDirectoryEntry(parent_id=self.teacher.id,department_id=self.platform.id))
        for row,title in ((self.teacher,'Exact source notice'),(self.platform,'Neighbour notice')):
            db.session.add(Announcement(school_id=self.school.id,department_id=row.id,title=title,
                url=row.list_url+'notice',published_at=datetime.utcnow()))
        db.session.commit()
        soup,_=self.render(self.path+f'&dept={self.teacher.id}')
        self.assertIn('Exact source notice',soup.get_text())
        self.assertNotIn('Neighbour notice',soup.select_one('#noticeList').get_text())
        sources=resolve_scope(self.user_id,dict(school_id=self.school.id,department_ids=[self.teacher.id]))
        self.assertEqual([row.id for row in sources],[self.teacher.id])

    def test_render_uses_captured_rules_when_another_connection_commits(self):
        real=read_structure;before=current_version(self.school.id)
        name=self.teacher.name;ident=self.teacher.id
        def capture_then_change(school,*args,**kwargs):
            captured=real(school,*args,**kwargs)
            with Session(db.engine) as writer:
                writer.execute(db.update(Department).where(Department.id==ident).values(name='Changed after capture'))
                writer.commit()
            db.session.expire_all()
            return captured
        with patch('backend.services.structure_snapshot.read_structure',side_effect=capture_then_change):
            soup,_=self.render()
        panel=soup.select_one('#sourcePanel')
        self.assertEqual(int(panel['data-structure-revision']),before['revision'])
        self.assertIn(name,panel.select_one(f'[data-department][value="{ident}"]').parent['title'])
        self.assertNotIn('Changed after capture',panel.get_text())

    def test_saved_notices_remain_filterable_after_unsubscribing(self):
        from backend.database.models import UserAnnouncementState
        article=Announcement.query.first()
        db.session.add(UserAnnouncementState(user_id=self.user_id,announcement_id=article.id,starred=True))
        db.session.delete(Subscription.query.filter_by(user_id=self.user_id,school_id=self.school.id).one())
        db.session.commit()
        soup,_=self.render(self.path+'&view=saved')
        self.assertIsNotNone(soup.select_one(f'#sourcePanel [data-department][value="{article.department_id}"]'))
        self.assertTrue(db.session.get(UserAnnouncementState,(self.user_id,article.id)).starred)

    def test_captured_policy_is_not_mutated_by_its_caller(self):
        supplied=deepcopy(POLICY)
        with patch('backend.services.discovery_policy.get_attention_policy',return_value=supplied):
            captured=read_structure(self.school)
        supplied['profiles'][:]=['undergraduate'];supplied['weights']['personnel']=0
        self.assertEqual(captured['attention_policy'],POLICY)

    def test_new_source_after_capture_waits_for_the_next_snapshot(self):
        from backend.services.inbox import filtered_inbox
        from werkzeug.datastructures import MultiDict
        captured=read_structure(self.school)
        newer=self.f.source('Later source',self.school.url+'/later/')
        notice=Announcement(school_id=self.school.id,department_id=newer.id,title='Later notice',
            url=newer.list_url+'notice',published_at=datetime.utcnow())
        db.session.add(notice);db.session.commit()
        args=MultiDict(dict(school=str(self.school.id),period='all'))
        self.assertNotIn(notice.id,[row.id for row in filtered_inbox(self.user_id,args,structure_capture=captured)])
        self.assertIn(notice.id,[row.id for row in filtered_inbox(self.user_id,args,structure_capture=read_structure(self.school))])

    def test_historical_organization_reading_scope_does_not_expand_its_children(self):
        from backend.services.inbox_refresh import resolve_scope
        old=Department(school_id=self.school.id,name='Historical row',kind='unit',list_url=self.school.url+'/old/')
        db.session.add(old);db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=old.id,department_id=self.teacher.id))
        db.session.add(Announcement(school_id=self.school.id,department_id=old.id,title='Historical organization notice',
            url=old.list_url+'notice',published_at=datetime.utcnow()))
        db.session.commit()
        soup,_=self.render(self.path+f'&dept={old.id}')
        self.assertIsNotNone(soup.select_one(f'[data-source-group="历史通知范围"] [value="{old.id}"]:checked'))
        self.assertIsNone(soup.select_one(f'[value="{self.teacher.id}"]:checked'))
        self.assertEqual(resolve_scope(self.user_id,dict(school_id=self.school.id,department_ids=[old.id])),[])

    def test_multiple_evidenced_owners_share_one_exact_source_scope(self):
        from backend.services.inbox import source_group_ids
        url=self.school.url+'/directory/'
        document=self.f.document(url,label='Isolated publisher proof fixture')
        self.f.fixture.publish([document])
        for index in (1,2):
            owner=Department(school_id=self.school.id,name=f'Fixture owner {index}',kind='unit',
                list_url=self.school.url+f'/owner-{index}/')
            db.session.add(owner);db.session.flush()
            identity=f'main #owner-{index}';entrance=f'main #column-{index}'
            proofs=[dict(reference_url=url,locator=locator,content_hash=document['content_hash'],
                observed_at=document['checked_at']) for locator in (identity,entrance)]
            assertion=dict(kind='explicit_publisher_identity',predicate='publication_owner',
                direction='publisher_to_source',identity_text=owner.name,identity_reference_url=url,
                identity_locator=identity,entry_reference_url=url,entry_locator=entrance,
                entry_kind='column_link',entry_url=self.teacher.list_url)
            db.session.add(DepartmentDirectoryEntry(parent_id=owner.id,department_id=self.teacher.id,
                facts_json=[dict(type='source_ownership',assertion=assertion,evidence=proofs)]))
        db.session.commit()
        capture=read_structure(self.school)
        self.assertEqual(len([e for e in capture['facts']['relations'] if e['type']=='source_ownership']),2)
        soup,_=self.render(self.path+f'&dept={self.teacher.id}')
        self.assertEqual(len(soup.select(f'[data-source-unit] [value="{self.teacher.id}"]:checked')),2)
        self.assertEqual(source_group_ids(self.school.id,'有依据的栏目归属',capture=capture),{self.teacher.id})


if __name__=='__main__':unittest.main()
