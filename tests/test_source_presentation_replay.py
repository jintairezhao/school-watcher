"""Replay retained real catalogue evidence; state rows are isolated test fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
from flask import Flask
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import (School,Department,DepartmentDirectoryEntry,User,
    Subscription,Announcement,UserRead,UserAnnouncementState)
from backend.services.runtime_catalog import RuntimeCatalog,pack
from backend.services.inbox import source_hierarchy

EVIDENCE=json.loads((Path(__file__).parent/'fixtures/source-presentation-cupk-evidence.json').read_text(encoding='utf-8'))

def seed_cupk(app, *, existing_school=None):
    school=existing_school or School(**EVIDENCE['schools'][0])
    if existing_school:
        school.name=EVIDENCE['schools'][0]['name'];school.url=EVIDENCE['schools'][0]['url']
    else: db.session.add(school)
    db.session.flush()
    for row in EVIDENCE['sources']: db.session.add(Department(**row))
    db.session.flush()
    for row in EVIDENCE['entries']: db.session.add(DepartmentDirectoryEntry(**row))
    db.session.commit()
    catalog=RuntimeCatalog(app.config['SOURCE_CATALOG_PATH'])
    with catalog.connect(write=True) as connection:
        for row in EVIDENCE['catalogs']:
            report={'site':{'site_key':row['site_key'],'name':row['name'],'root_url':row['root_url']},
                    'states':{'fetched':len(row['pages']),'pending':0},'accepted':False}
            cursor=connection.execute('INSERT INTO catalog_sites (site_key,name,root_url,updated_at,report_gzip,paths_gzip,candidates_gzip) VALUES(?,?,?,?,?,?,?)',
                (row['site_key'],row['name'],row['root_url'],row['updated_at'],pack(report),pack(row['paths']),pack([])))
            for page in row['pages']:
                connection.execute('INSERT INTO catalog_pages (site_id,url,page_gzip,nodes_gzip) VALUES(?,?,?,?)',
                    (cursor.lastrowid,page['url'],pack(page),pack([])))
    return school

class SourcePresentationReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='source-presentation-')
        self.app=Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',SQLALCHEMY_TRACK_MODIFICATIONS=False,
                               SOURCE_CATALOG_PATH=str(Path(self.temp.name)/'catalog.db'))
        db.init_app(self.app);self.ctx=self.app.app_context();self.ctx.push();db.create_all()
        self.school=seed_cupk(self.app)

    def tearDown(self):
        db.session.remove();db.engine.dispose();self.ctx.pop();self.temp.cleanup()

    def tree(self,**kwargs):
        with self.app.test_request_context('/'), patch('backend.ai.providers.complete',side_effect=AssertionError('Paid request in projection')):
            return source_hierarchy(Department.query.all(),directory_entries=DepartmentDirectoryEntry.query.all(),available_only=True,**kwargs)

    def test_retained_real_columns_use_one_unit_and_distinguishable_navigation_labels(self):
        tree=self.tree()
        teaching=[u for u in tree['组织机构'] if u['name']=='教务部']
        self.assertEqual(len(teaching),1)
        labels={c['department'].id:c['label'] for c in teaching[0]['own_columns']}
        self.assertEqual(labels[2],'通知公告')
        self.assertEqual(labels[3],'教学运行 / 通知公告')
        self.assertEqual(labels[4],'国际教育 / 通知公告')
        self.assertEqual(labels[5],'教学研究 / 通知公告')
        colleges=[u for u in tree['院系设置'] if u['name']=='化工与环境学院']
        self.assertEqual(len(colleges),1)
        self.assertEqual({c['department'].id for c in colleges[0]['own_columns']},{14,66,67,68,69})
        # The unconfigured pager lead is preserved in storage, not another choice.
        self.assertIsNotNone(db.session.get(Department,88))

    def test_live_reciprocal_navigation_places_aid_under_its_roster_parent(self):
        tree=self.tree()
        parent=next(u for u in tree['组织机构'] if u['department_id']==1143)
        aid=next(u for u in parent['children'] if u['department_id']==1148)
        self.assertEqual({c['department'].id for c in aid['own_columns']},{39})
        self.assertNotIn(39,{c['department'].id for u in tree.get('其他信息来源',[]) for c in u['columns']})

    def test_actual_primary_navigation_keeps_other_student_work_columns_under_department(self):
        tree=self.tree()
        parent=next(u for u in tree['组织机构'] if u['department_id']==1143)
        self.assertTrue({35,36,37,38,39,40}.issubset({c['department'].id for c in parent['columns']}))
        labels={c['department'].id:c['label'] for c in parent['own_columns']}
        self.assertEqual(labels[36],'共青团')
        self.assertEqual(labels[38],'安全保卫 / 通知公告')
        self.assertEqual(labels[40],'心理健康 / 通知公告')

    def test_projection_preserves_ids_rules_subscriptions_and_reader_states(self):
        user=User(username='fixture-reader',password_hash='unused');db.session.add(user);db.session.flush()
        subscription=Subscription(user_id=user.id,school_id=1,department_ids=[39,88]);db.session.add(subscription)
        notice=Announcement(school_id=1,department_id=39,title='隔离回归通知',url='https://www.cupk.edu.cn/xszz/test-fixture/',content_html='本地回归正文')
        db.session.add(notice);db.session.flush()
        db.session.add_all([UserRead(user_id=user.id,announcement_id=notice.id),
                           UserAnnouncementState(user_id=user.id,announcement_id=notice.id,starred=True)])
        db.session.commit()
        before=[(r.id,r.name,r.list_url,r.list_selector) for r in Department.query.order_by(Department.id)]
        self.tree(retained_ids=subscription.department_ids)
        self.assertEqual(before,[(r.id,r.name,r.list_url,r.list_selector) for r in Department.query.order_by(Department.id)])
        self.assertEqual(subscription.department_ids,[39,88]);self.assertEqual(UserRead.query.count(),1)
        self.assertTrue(UserAnnouncementState.query.one().starred);self.assertEqual(notice.content_html,'本地回归正文')
        self.assertFalse(db.session.dirty)

if __name__=='__main__': unittest.main()
