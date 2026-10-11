"""Display-only compatibility for public legacy sources in a frozen package.

The six source identities reproduce the installed SJTU screenshot. The full
developer seed and the local discovery catalogue are deliberately absent; the
test does not visit the university or make model requests.
"""
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from backend.database.db import db
from backend.database.models import (Announcement, Department, DepartmentDirectoryEntry,
    School, Subscription, User, UserAnnouncementState, UserRead)
from backend.services.inbox import source_hierarchy
from backend.services import starter_catalog
from backend.services.source_inventory import canonical_url
from desktop.resources import application_data

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = 'config/legacy-source-display-paths.json'
PUBLIC_CATALOG = json.loads((ROOT / 'config/desktop-schools.json').read_bytes())
SJTU = next(school for school in PUBLIC_CATALOG['schools']
    if canonical_url(school['url']) == 'https://www.sjtu.edu.cn/')
INSTALLED_IDENTITIES = (
    (315, '招生信息', '心理学院', 'https://psychology.sjtu.edu.cn/zsxx.html'),
    (316, '研究生招生', '物理与天文学院', 'https://www.physics.sjtu.edu.cn/yjszs.html'),
    (322, '学生培养', '心理学院', 'https://psychology.sjtu.edu.cn/pyfa.html'),
    (355, '通知公告', '物理与天文学院', 'https://www.physics.sjtu.edu.cn/'),
    (358, '通知公告', '心理学院', 'https://psychology.sjtu.edu.cn/tzgg.html'),
    (372, '新闻资讯', '心理学院', 'https://psychology.sjtu.edu.cn/'),
)


class LegacySourceDisplayPathsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='legacy-source-display-')
        self.frozen = Path(self.temp.name) / 'frozen'
        self.manifest = application_data(ROOT, Path(self.temp.name) / 'build')
        self.resource_names = {str(Path(dest) / Path(source).name).replace('\\', '/')
            for source, dest in self.manifest}
        # Copy from the actual packaging manifest, rather than an invented
        # fixture that would accidentally retain the excluded full catalogue.
        for source, dest in self.manifest:
            target = self.frozen / dest / Path(source).name
            if str(Path(dest) / Path(source).name).replace('\\', '/') == RESOURCE:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI='sqlite://',
            SQLALCHEMY_TRACK_MODIFICATIONS=False, DESKTOP_MODE=True,
            SOURCE_CATALOG_PATH=str(Path(self.temp.name) / 'missing-catalog.sqlite3'))
        db.init_app(self.app)
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.root_patch = patch.object(starter_catalog, 'ROOT_DIR', self.frozen)
        self.root_patch.start(); starter_catalog._read.cache_clear()

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.context.pop()
        self.root_patch.stop(); starter_catalog._read.cache_clear(); self.temp.cleanup()

    def seed(self):
        school = School(id=5, name='上海交通大学', url=SJTU['url'])
        db.session.add(school)
        rows = []
        for ident, name, group, address in INSTALLED_IDENTITIES:
            saved = next(entry for entry in SJTU['departments']
                if (entry['name'], entry['group_name'], canonical_url(entry['list_url'])) ==
                   (name, group, canonical_url(address)))
            row = Department(id=ident, school_id=school.id, kind='column', name=name,
                group_name=group, list_url=address, list_selector=saved['list_selector'])
            rows.append(row); db.session.add(row)
        db.session.commit()
        return school, rows

    def test_packaged_legacy_columns_keep_real_paths_without_seed_or_catalogue(self):
        school, rows = self.seed()
        user = User(username='legacy-display-reader', password_hash='unused')
        db.session.add(user); db.session.flush()
        subscription = Subscription(user_id=user.id, school_id=school.id,
            department_ids=[row.id for row in rows])
        notice = Announcement(school_id=school.id, department_id=rows[0].id,
            title='显示兼容保留用户状态', url=rows[0].list_url)
        db.session.add_all([subscription, notice]); db.session.flush()
        read = UserRead(user_id=user.id, announcement_id=notice.id)
        saved = UserAnnouncementState(user_id=user.id, announcement_id=notice.id, starred=True)
        db.session.add_all([read, saved]); db.session.commit()
        before = ([(row.id, row.name, row.group_name, row.list_url, row.list_selector) for row in rows],
            list(subscription.department_ids), read.announcement_id, saved.announcement_id, saved.starred)
        self.assertEqual(DepartmentDirectoryEntry.query.count(), 0)
        self.assertFalse(Path(self.app.config['SOURCE_CATALOG_PATH']).exists())
        self.assertEqual(starter_catalog.catalog()['schools'], [])
        with self.app.test_request_context('/'), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No AI in display')), \
             patch('backend.ai.providers.complete', side_effect=AssertionError('No paid request')), \
             patch('backend.scraper.acquisition.http_transport.fetch_http', side_effect=AssertionError('No website read')):
            tree = source_hierarchy(rows, rows, [])
        self.assertEqual(set(tree), {'院系设置'})
        units = {unit['name']: unit for unit in tree['院系设置']}
        self.assertEqual(set(units), {'心理学院', '物理与天文学院'})
        self.assertEqual({column['department'].id for column in units['心理学院']['columns']},
            {315, 322, 358, 372})
        self.assertEqual({column['label'] for column in units['心理学院']['columns']},
            {'人才培养 / 招生信息', '学生培养', '新闻动态 / 通知公告', '新闻资讯'})
        self.assertEqual({column['department'].id for column in units['物理与天文学院']['columns']}, {316, 355})
        self.assertEqual({column['label'] for column in units['物理与天文学院']['columns']},
            {'研究生招生', '通知公告'})
        self.assertTrue(all(unit['expandable'] and not unit['children'] for unit in units.values()))
        after = ([(row.id, row.name, row.group_name, row.list_url, row.list_selector) for row in rows],
            list(subscription.department_ids), read.announcement_id, saved.announcement_id, saved.starred)
        self.assertEqual(before, after)
        self.assertFalse(db.session.dirty)
        self.assertFalse(Path(self.app.config['SOURCE_CATALOG_PATH']).exists())

    def test_unmatched_school_name_group_address_or_list_region_stays_unknown(self):
        school, rows = self.seed()
        original = rows[0]
        fields = {key: getattr(original, key) for key in
            ('name', 'group_name', 'list_url', 'list_selector')}
        self.assertTrue(starter_catalog.placements(school.url, original))
        for changes in ({'name': '另一个栏目'}, {'group_name': '另一学院'},
                        {'list_url': 'https://psychology.sjtu.edu.cn/other.html'},
                        {'list_selector': fields['list_selector'] + ' .another-region'}):
            with self.subTest(changes=changes):
                different = SimpleNamespace(**{**fields, **changes})
                self.assertEqual(starter_catalog.placements(school.url, different), [])
        self.assertEqual(starter_catalog.placements('https://other-university.edu.cn/', original), [])

    def test_packaged_resource_is_display_only_and_cannot_seed_sources(self):
        self.assertIn(RESOURCE, self.resource_names)
        self.assertNotIn('config/desktop-schools.json', self.resource_names)
        self.assertNotIn('config/schools.yaml', self.resource_names)
        compact = json.loads((self.frozen / RESOURCE).read_bytes())
        self.assertEqual(compact['origin'], 'desktop-schools.json:2026-09-27')
        found = 0
        for school in compact['schools']:
            original = next(item for item in PUBLIC_CATALOG['schools']
                if canonical_url(item['url']) == school['url'])
            self.assertEqual(set(school), {'url', 'sources'})
            for row in school['sources']:
                self.assertEqual(set(row), {'name', 'group_name', 'list_url',
                    'list_selector_sha256', 'placements'})
                matches = [entry for entry in original['departments']
                    if entry['name'] == row['name'] and entry['group_name'] == row['group_name']
                    and canonical_url(entry['list_url']) == row['list_url']
                    and hashlib.sha256(entry['list_selector'].encode()).hexdigest() == row['list_selector_sha256']]
                self.assertEqual(len(matches), 1)
                self.assertEqual(row['placements'], matches[0]['placements'])
                self.assertTrue(row['placements']); found += 1
        self.assertEqual(found, 199)
        before = (School.query.count(), Department.query.count())
        starter_catalog.install()
        self.assertEqual((School.query.count(), Department.query.count()), before)


if __name__ == '__main__':
    unittest.main()
