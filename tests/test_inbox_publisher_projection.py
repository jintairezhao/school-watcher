"""Replay real installed source identities and retained public website metadata.

The independent v14 Nankai/Shandong API runs installed columns without separate
unit rows. The inbox must consume existing publisher evidence for those columns;
hand-created unit fixtures alone do not exercise that production path.
"""
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from backend.database.db import db
from backend.database.models import (Announcement, Department, DepartmentDirectoryEntry,
    School, Subscription, User, UserAnnouncementState, UserRead)
from backend.services.inbox import source_hierarchy
from backend.services.runtime_catalog import RuntimeCatalog, pack
from backend.services.source_channels import channels_for
from backend.services.source_inventory import site_key

EVIDENCE = json.loads((Path(__file__).parent / 'fixtures' /
    'source_sidebar_publisher_evidence.json').read_bytes())


class InboxPublisherProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='inbox-publisher-')
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SOURCE_CATALOG_PATH=str(Path(self.temp.name) / 'catalog.sqlite3'))
        db.init_app(self.app)
        self.context = self.app.app_context(); self.context.push(); db.create_all()

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.context.pop(); self.temp.cleanup()

    def seed(self, run):
        school = School(**run['school'])
        db.session.add(school)
        rows = []
        for saved in run['sources']:
            row = Department(school_id=school.id, **saved)
            rows.append(row); db.session.add(row)
        db.session.commit()
        catalog = RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH'])
        key = site_key(school.url)
        with catalog.connect(write=True) as connection:
            cursor = connection.execute('INSERT INTO catalog_sites '
                '(site_key,name,root_url,updated_at,report_gzip,paths_gzip,candidates_gzip) '
                'VALUES(?,?,?,?,?,?,?)', (key, school.name, school.url, 'retained-v14',
                    pack({}), pack(run['paths']), pack([])))
            for page in run['pages']:
                connection.execute('INSERT INTO catalog_pages '
                    '(site_id,url,page_gzip,nodes_gzip) VALUES(?,?,?,?)',
                    (cursor.lastrowid, page['url'], pack(page), pack([])))
        return rows

    def checked_tree(self, rows):
        with self.app.test_request_context('/'), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No AI in projection')), \
             patch('backend.ai.providers.complete', side_effect=AssertionError('No paid request')), \
             patch('backend.scraper.acquisition.http_transport.fetch_http', side_effect=AssertionError('No website read')):
            descriptors = channels_for(rows)
            tree = source_hierarchy(rows, rows, [])
        return descriptors, tree

    def test_actual_api_columns_without_unit_rows_receive_observed_publishers(self):
        for run in EVIDENCE['runs']:
            db.session.remove(); db.drop_all(); db.create_all()
            Path(self.app.config['SOURCE_CATALOG_PATH']).unlink(missing_ok=True)
            with self.subTest(run=run['run']):
                rows = self.seed(run)
                self.assertTrue(all(row.kind == 'column' for row in rows))
                self.assertEqual(DepartmentDirectoryEntry.query.count(), 0)
                descriptors, tree = self.checked_tree(rows)
                units = [unit for group in tree.values() for unit in group]
                expected = {row['publisher_key']: row['publisher_label']
                            for row in run['expected_publishers']}
                # Retained evidence has old URL fallback/suffix labels. Those
                # addresses remain identities, not human department names.
                for unit in units:
                    descriptor = descriptors[unit['columns'][0]['department'].id]
                    self.assertIn(descriptor['publisher_key'], expected)
                    if descriptor['basis'] == 'official_page':
                        self.assertFalse(unit['expandable'])
                        self.assertEqual(unit['name'], unit['columns'][0]['label'])
                    else:
                        self.assertTrue(unit['expandable'])
                        self.assertEqual(unit['name'], re.sub(r'（[^（）]*\.edu[^（）]*）$', '', expected[unit['key']]))
                for unit in units:
                    for column in unit['own_columns']:
                        source = column['department']
                        self.assertEqual(column['label'], source.name)
                        if unit['expandable']:
                            self.assertEqual(descriptors[source.id]['publisher_key'], unit['key'])
                self.assertCountEqual([column['department'].id for unit in units
                    for column in unit['columns']], [row.id for row in rows])

    def test_shared_page_keeps_distinct_columns_and_homonymous_publishers_stay_separate(self):
        run = EVIDENCE['runs'][1]
        rows = self.seed(run)
        descriptors, tree = self.checked_tree(rows)
        units = [unit for group in tree.values() for unit in group]
        grad = next(unit for unit in units if unit['name'] == '山东大学研究生院')
        self.assertEqual(rows[5].list_url, rows[6].list_url)
        self.assertEqual({column['department'].id for column in grad['columns']}, {6, 7})
        self.assertEqual({column['label'] for column in grad['columns']}, {'学子 风采', '政策 法规'})
        admissions = [unit for unit in units if unit['name'].startswith('山东大学研究生招生信息网')]
        self.assertEqual(len(admissions), 3)
        self.assertEqual(len({unit['key'] for unit in admissions}), 3)
        self.assertEqual({unit['name'] for unit in admissions}, {'山东大学研究生招生信息网'})
        # Even equal column labels must not merge independently observed sites.
        rows[0].name = rows[1].name = '通知公告'; db.session.commit()
        descriptors, tree = self.checked_tree(rows)
        self.assertNotEqual(descriptors[1]['publisher_key'], descriptors[2]['publisher_key'])
        self.assertCountEqual([column['department'].id for group in tree.values()
            for unit in group for column in unit['columns']], range(1, 8))

    def test_projection_preserves_subscriptions_read_saved_and_source_ids(self):
        rows = self.seed(EVIDENCE['runs'][0])
        user = User(username='projection-reader', password_hash='unused')
        db.session.add(user); db.session.flush()
        subscription = Subscription(user_id=user.id, school_id=rows[0].school_id,
            department_ids=[row.id for row in rows])
        notice = Announcement(school_id=rows[0].school_id, department_id=rows[0].id,
            title='来源投影状态回归', url=rows[0].list_url)
        db.session.add_all([subscription, notice]); db.session.flush()
        read = UserRead(user_id=user.id, announcement_id=notice.id)
        saved = UserAnnouncementState(user_id=user.id, announcement_id=notice.id, starred=True)
        db.session.add_all([read, saved]); db.session.commit()
        before = ([(row.id, row.name, row.list_url, row.group_name) for row in rows],
            subscription.id, list(subscription.department_ids), notice.department_id,
            read.user_id, read.announcement_id, saved.user_id, saved.announcement_id, saved.starred)
        descriptors, tree = self.checked_tree(rows)
        after = ([(row.id, row.name, row.list_url, row.group_name) for row in rows],
            subscription.id, list(subscription.department_ids), notice.department_id,
            read.user_id, read.announcement_id, saved.user_id, saved.announcement_id, saved.starred)
        self.assertEqual(before, after)
        self.assertFalse(db.session.dirty)
        unknown = next(unit for group in tree.values() for unit in group
            if any(column['department'].id == 3 for column in unit['columns']))
        self.assertEqual(descriptors[3]['basis'], 'official_page')
        self.assertEqual(unknown['name'], '信息公开制度')
        self.assertFalse(unknown['expandable'])
        self.assertEqual(unknown['columns'][0]['label'], '信息公开制度')

    def test_legacy_owner_own_source_and_columns_keep_one_unit_in_real_app_context(self):
        school = School(id=1, name='旧配置回归学校', url='https://legacy.example/')
        db.session.add(school)
        rows = [Department(id=ident, school_id=school.id, name=name,
            list_url=url, group_name='组织机构') for ident, name, url in (
                (200, '研究生院', 'https://legacy.example/graduate/'),
                (201, '研究生院-招生通知', 'https://legacy.example/graduate/admission/'),
                (202, '研究生院-学位通知', 'https://legacy.example/graduate/degree/'))]
        db.session.add_all(rows); db.session.commit()
        descriptors, tree = self.checked_tree(rows)
        self.assertEqual(len(tree['组织机构']), 1)
        unit = tree['组织机构'][0]
        self.assertEqual((unit['key'], unit['name'], unit['expandable']), ('200', '研究生院', True))
        self.assertEqual({column['department'].id for column in unit['columns']}, {200, 201, 202})
        self.assertEqual({column['label'] for column in unit['columns']}, {'官网信息', '招生通知', '学位通知'})
        # Known descendants preserve the owner's identity even in a restricted
        # subscription, without adding the unselected descendant sources.
        with self.app.test_request_context('/'):
            restricted = source_hierarchy([rows[0]], rows)['组织机构']
        self.assertEqual([unit['key'] for unit in restricted], ['200'])
        self.assertEqual([column['department'].id for column in restricted[0]['columns']], [200])


if __name__ == '__main__':
    unittest.main()
