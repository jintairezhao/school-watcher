"""Official rosters must reach selectable options, even with existing source rules."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import Department, School, Subscription, User, Announcement
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page

ROOT_URL = 'https://www.uestc.edu.cn/'
DIRECTORY_URL = ROOT_URL + 'xybm/jxkydw_yjjg.htm'
DIRECTORY_NAME = '教学科研单位、研究机构'


class DirectoryOptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'directory-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite://', 'SOURCE_CATALOG_PATH': str(folder / 'catalog.db')})
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.school = School(name='电子科技大学', url=ROOT_URL, subscriber_count=1)
        self.user = User(username='reader', password_hash='unused')
        db.session.add_all([self.school, self.user]); db.session.flush()
        self.parent = Department(school_id=self.school.id, name=DIRECTORY_NAME,
                                 group_name='学院部门', list_url=DIRECTORY_URL)
        self.configured = Department(school_id=self.school.id, name='学生工作',
                                     list_url=ROOT_URL + 'students', list_selector='ul.news li')
        db.session.add_all([self.parent, self.configured]); db.session.flush()
        self.sub = Subscription(user_id=self.user.id, school_id=self.school.id)
        db.session.add(self.sub); db.session.commit()
        inv = Inventory(folder / 'inventory.db')
        self.key = inv.ensure_site(self.school.name, ROOT_URL)
        inv.enqueue(self.key, DIRECTORY_URL, DIRECTORY_NAME, 'directory', 1, [], 'school_domain')
        report = inv.report(self.key)
        page = next(p for p in report['pages'] if p['url'] == DIRECTORY_URL)
        html = (Path(__file__).parent / 'fixtures/uestc_academic_directory.html').read_text(encoding='utf-8')
        inspect_page(inv, report['site'], page, fetcher=lambda url: {'html': html, 'url': url, 'status': 200})
        self.catalog = RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH'])
        self.catalog.publish(inv, self.key)

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop(); self.temp.cleanup()

    def sync(self):
        from backend.services.directory_options import sync_directory_options
        return sync_directory_options(self.school, self.catalog)

    def test_real_roster_is_selectable_in_official_order_without_navigation_noise(self):
        from backend.services.inbox import source_hierarchy
        from backend.services.directory_options import directory_entries_for
        self.sync()
        rows = Department.query.all()
        tree = source_hierarchy(rows, directory_entries=directory_entries_for(self.school.id))
        unit = tree['学院部门'][0]
        names = [c['label'] for c in unit['columns']]
        self.assertEqual(len(names), 43)
        self.assertEqual(names[:2], ['信息与通信工程学院', '电子科学与工程学院'])
        self.assertIn('计算机科学与工程学院（网络空间安全学院）', names)
        self.assertNotIn('本部门通知', names)
        self.assertTrue(unit['directory'])
        unavailable = next(c for c in unit['columns'] if c['label'] == '电子信息智能研究院')
        self.assertFalse(unavailable['department'].list_url)

    def test_sync_is_idempotent_preserves_records_and_supports_multiple_memberships(self):
        self.sync()
        original = [(d.id, d.name, d.list_url, d.list_selector) for d in Department.query.all()]
        self.sync()
        self.assertEqual(original, [(d.id, d.name, d.list_url, d.list_selector) for d in Department.query.all()])
        self.assertEqual(self.configured.list_selector, 'ul.news li')
        second = Department(school_id=self.school.id, name='另一份官方目录', group_name='学院部门', list_url=DIRECTORY_URL)
        db.session.add(second); db.session.commit()
        self.sync()
        from backend.services.directory_options import directory_entries_for
        entries = directory_entries_for(self.school.id)
        self.assertEqual(len(entries), 86)
        self.assertEqual(Department.query.count(), 46)
        self.assertIsNone(self.sub.department_ids)

    def test_existing_configured_column_does_not_block_worker_roster_sync(self):
        from backend.worker import dispatch
        with patch('backend.scraper.engine.scrape_school', return_value=SimpleNamespace(status='success', new_count=0)):
            dispatch('scrape', {'school_id': self.school.id})
        self.assertIsNotNone(Department.query.filter_by(name='信息与通信工程学院').first())

    def test_publication_config_cannot_rename_an_official_roster_unit(self):
        from backend.services.source_catalog import apply_source_configs
        self.sync()
        child = Department.query.filter_by(name='信息与通信工程学院').one()
        ident = child.id
        apply_source_configs(self.school.id, [{'name': '通知公告', 'list_url': child.list_url,
                             'list_selector': 'ul.notices li', 'group_name': child.name}])
        self.assertEqual(db.session.get(Department, ident).name, '信息与通信工程学院')
        self.sync()
        self.assertEqual(Department.query.filter_by(name='信息与通信工程学院').count(), 1)

    def test_collector_skips_directory_pages_and_collects_parent_subscription_children(self):
        from backend.scraper.engine import scrape_school
        self.sync()
        self.sub.department_ids = [self.parent.id]
        db.session.commit()
        with patch('backend.scraper.engine.scrape_department', return_value=(0, 1)) as scrape:
            scrape_school(self.school)
        ids = [call.args[0].id for call in scrape.call_args_list]
        self.assertEqual(len(ids), 42)
        self.assertNotIn(self.parent.id, ids)
        self.assertNotIn(self.configured.id, ids)

    def test_parent_subscription_and_old_url_include_children_but_not_other_users(self):
        self.sync()
        child = Department.query.filter_by(name='信息与通信工程学院').one()
        self.sub.department_ids = [self.parent.id]
        db.session.add(Announcement(school_id=self.school.id, department_id=child.id, title='信通学院测试通知'))
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.user.id
        response = client.get(f'/?school={self.school.id}&dept={self.parent.id}&period=all')
        self.assertEqual(response.status_code, 200)
        self.assertIn('信通学院测试通知', response.text)
        self.assertIn('信息与通信工程学院', response.text)
        self.assertIn('43 个栏目', response.text)
        self.assertIn('官网未提供链接', response.text)
        self.assertNotIn('信通学院测试通知', self.app.test_client().get('/?period=all').text)


if __name__ == '__main__':
    unittest.main()
