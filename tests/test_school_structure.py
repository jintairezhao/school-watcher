"""A successful onboarding includes a usable, correctly owned subscription tree."""
from pathlib import Path
import unittest

import test_direct_onboarding as direct_fixture
HTML = direct_fixture.HTML
from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry, School, Subscription
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services.source_inventory import Inventory
from backend.services.school_structure import sync_official_structure

ROOT = 'https://example.edu.cn/'
CS = 'https://cs.example.edu.cn/'
LANG = 'https://lang.example.edu.cn/'


class SchoolStructureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = direct_fixture.DirectOnboardingTests(); self.fixture.setUp()
        self.school = db.session.get(School, self.fixture.school_id)
        self.inventory = Inventory(Path(self.fixture.temp.name) / 'structure.db')
        self.key = self.inventory.ensure_site(self.school.name, ROOT)
        self.catalog = RuntimeCatalog(self.fixture.app.config['SOURCE_CATALOG_PATH'])
        self.pages = {ROOT: '<title>测试大学</title><nav><a href="/colleges/">院系设置</a></nav>',
            ROOT + 'colleges/': '<h1>院系设置</h1><ul><li><a href="' + CS + '">计算机学院</a></li>'
                '<li><a href="' + LANG + '">外国语学院</a></li></ul>',
            CS: '<title>测试大学计算机学院</title><nav><a href="/notices/">通知公告</a>'
                '<a href="' + LANG + 'notices/">外国语学院通知</a></nav>',
            LANG: '<title>测试大学外国语学院</title><nav><a href="/notices/">通知公告</a></nav>',
            CS + 'notices/': HTML, LANG + 'notices/': HTML}

    def tearDown(self):
        self.fixture.tearDown()

    def publish(self):
        for url, kind, label in [(ROOT, 'root', '测试大学'), (ROOT + 'colleges/', 'directory', '院系设置'),
                (CS, 'unit', '计算机学院'), (LANG, 'unit', '外国语学院'),
                (CS + 'notices/', 'channel', '通知公告'), (LANG + 'notices/', 'channel', '通知公告')]:
            self.inventory.enqueue(self.key, url, label, kind, 1, [], 'school_domain')
            inspect_page(self.inventory, self.inventory.report(self.key)['site'], self.inventory.get_page(self.key, url),
                fetcher=lambda address: {'url': address, 'status': 200, 'html': self.pages[address]})
        self.catalog.publish(self.inventory, self.key)
        return sync_official_structure(self.school, self.catalog)

    def connect(self, url):
        from backend.services.source_onboarding import onboard_page
        return onboard_page({'school_id': self.school.id, 'url': url},
            fetcher=lambda address, purpose: HTML if address == url else self.fixture.fetch(address, purpose))

    def tree(self):
        from backend.services.inbox import source_hierarchy
        from backend.services.directory_options import directory_entries_for
        return source_hierarchy(self.school.departments.all(), directory_entries=directory_entries_for(self.school.id))

    def test_first_school_builds_selectable_units_before_any_column_is_ready(self):
        self.publish()
        from backend.services.inbox_refresh import subscribed_sources
        self.assertEqual({d.name for d in Department.query.filter_by(kind='unit')}, {'计算机学院', '外国语学院'})
        self.assertEqual(subscribed_sources(self.school), [])
        self.assertEqual({u['name'] for u in self.tree()['院系设置']}, {'计算机学院', '外国语学院'})
        self.assertTrue(all(not u['columns'] for u in self.tree()['院系设置']))

    def test_directory_excludes_repeated_header_and_footer_units(self):
        self.pages[ROOT + 'colleges/'] = (
            '<div class="header"><ul class="hd-nav"><li><a href="/academy/">上大书院</a>'
            '<ul><li><a href="/academy/intro/">书院概况</a></li></ul></li></ul></div>'
            '<div class="big-nav"><h2>上大书院</h2><ul><li><a href="/academy/intro/">书院概况</a>'
            '</li></ul><h2>人才中心</h2><ul><li><a href="/talent/">人才研究院</a></li></ul></div>'
            '<footer><h2>校友中心</h2></footer>' + self.pages[ROOT + 'colleges/'])
        self.publish()
        self.assertEqual({d.name for d in Department.query.filter_by(kind='unit')},
                         {'计算机学院', '外国语学院'})

    def test_nested_list_in_directory_body_preserves_administrative_parent(self):
        self.pages[ROOT + 'colleges/'] = (
            '<main><h1>院系设置</h1><ul><li><span>科学学院</span><ul><li><a href="' + CS + '">'
            '计算机学院</a></li></ul></li><li><a href="' + LANG + '">外国语学院</a></li></ul></main>')
        self.publish()
        parent = next(u for u in self.tree()['院系设置'] if u['name'] == '科学学院')
        self.assertEqual([u['name'] for u in parent['children']], ['计算机学院'])

    def test_directory_cards_preserve_official_subject_groups(self):
        self.pages[ROOT + 'colleges/'] = (
            '<main><h1>院系设置</h1><div class="box"><div class="tit">理工类</div><div class="con">'
            '<div><a href="' + CS + '">计算机学院</a></div></div></div>'
            '<div class="box"><div class="tit">人文社科类</div><div class="con">'
            '<div><a href="' + LANG + '">外国语学院</a></div><ul><li>文化研究院</li></ul></div></div></main>')
        # Many university pages embed both desktop and mobile copies.
        self.pages[ROOT + 'colleges/'] *= 2
        self.publish()
        groups = {u['name']: u for u in self.tree()['院系设置']}
        self.assertEqual(set(groups), {'理工类', '人文社科类'})
        self.assertEqual(len(self.tree()['院系设置']), 2)
        self.assertEqual(Department.query.filter_by(kind='unit').count(), 3)
        self.assertEqual({u['name'] for u in groups['理工类']['children']}, {'计算机学院'})
        self.assertEqual({u['name'] for u in groups['人文社科类']['children']}, {'外国语学院', '文化研究院'})

    def test_same_named_columns_stay_under_their_own_units_despite_cross_links(self):
        self.publish()
        self.connect(CS + 'notices/'); self.connect(LANG + 'notices/')
        tree = self.tree()['院系设置']
        self.assertEqual(len(tree), 2)
        for unit in tree:
            self.assertEqual(len(unit['own_columns']), 1)
            source = unit['own_columns'][0]['department']
            self.assertEqual(source.list_url, (CS if unit['name'] == '计算机学院' else LANG) + 'notices/')

    def test_nested_unit_remains_below_its_parent_and_inherits_parent_subscription(self):
        self.pages[ROOT + 'colleges/'] = ('<h1>院系设置</h1><table><tr><th>学院</th><th>下属单位</th></tr>'
            '<tr><td>科学学院</td><td><a href="' + CS + '">计算机学院</a></td></tr></table>'
            '<ul><li><a href="' + LANG + '">外国语学院</a></li></ul>')
        self.publish()
        parent = Department.query.filter_by(kind='unit', name='科学学院').one()
        sub = Subscription.query.one(); sub.department_ids = [parent.id]; db.session.commit()
        connected = self.connect(CS + 'notices/')
        unit = next(u for u in self.tree()['院系设置'] if u['name'] == '科学学院')
        self.assertEqual([u['name'] for u in unit['children']], ['计算机学院'])
        self.assertEqual(len(unit['columns']), 1)
        self.assertEqual(unit['own_columns'], [])
        from backend.services.inbox_refresh import subscribed_sources
        self.assertEqual([d.id for d in subscribed_sources(self.school, sub.department_ids)], connected['department_ids'])

    def test_same_named_columns_inside_one_unit_keep_their_official_navigation_context(self):
        self.pages[CS] = ('<title>测试大学计算机学院</title><header><div class="c-header_lib_s">'
            '<a href="/teaching/">本科生教育</a><ul><li><a href="/notices/">通知公告</a></li></ul></div>'
            '<div class="c-header_lib_s"><a href="/graduate/">研究生教育</a><ul><li>'
            '<a href="/graduate/notices/">通知公告</a></li></ul></div></header>')
        self.publish()
        address = CS + 'graduate/notices/'
        self.inventory.enqueue(self.key, address, '通知公告', 'channel', 2, [], 'school_domain')
        inspect_page(self.inventory, self.inventory.report(self.key)['site'], self.inventory.get_page(self.key, address),
            fetcher=lambda url: {'url': url, 'html': HTML, 'status': 200})
        self.catalog.publish(self.inventory, self.key)
        self.connect(CS + 'notices/'); self.connect(address)
        unit = next(u for u in self.tree()['院系设置'] if u['name'] == '计算机学院')
        self.assertEqual({c['label'] for c in unit['own_columns']}, {'本科生教育 / 通知公告', '研究生教育 / 通知公告'})

    def test_department_subscription_inherits_later_columns_but_not_other_department(self):
        self.publish()
        unit = Department.query.filter_by(kind='unit', name='计算机学院').one()
        sub = Subscription.query.one(); sub.department_ids = [unit.id]; db.session.commit()
        first = self.connect(CS + 'notices/')
        self.connect(LANG + 'notices/')
        from backend.services.inbox_refresh import subscribed_sources
        self.assertEqual([d.id for d in subscribed_sources(self.school, sub.department_ids)], first['department_ids'])
        self.assertEqual(sub.department_ids, [unit.id])

    def test_republish_preserves_ids_subscriptions_and_memberships(self):
        self.publish(); self.connect(CS + 'notices/')
        before = [(d.id, d.structure_key, d.name) for d in Department.query.order_by(Department.id)]
        memberships = {(e.parent_id, e.department_id) for e in DepartmentDirectoryEntry.query.all()}
        sync_official_structure(self.school, self.catalog)
        self.assertEqual(before, [(d.id, d.structure_key, d.name) for d in Department.query.order_by(Department.id)])
        self.assertEqual(memberships, {(e.parent_id, e.department_id) for e in DepartmentDirectoryEntry.query.all()})

    def test_real_academic_directory_keeps_every_unit_including_missing_links(self):
        root = 'https://www.uestc.edu.cn/'
        address = root + 'xybm/jxkydw_yjjg.htm'
        self.school.url, self.school.name = root, '电子科技大学'; db.session.commit()
        key = self.inventory.ensure_site(self.school.name, root)
        self.inventory.enqueue(key, address, '教学科研单位、研究机构', 'directory', 1, [], 'school_domain')
        html = (Path(__file__).parent / 'fixtures/uestc_academic_directory.html').read_text(encoding='utf-8')
        inspect_page(self.inventory, self.inventory.report(key)['site'], self.inventory.get_page(key, address),
            fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        self.catalog.publish(self.inventory, key)
        sync_official_structure(self.school, self.catalog)
        units = Department.query.filter_by(kind='unit').all()
        self.assertEqual(len(units), 43)
        self.assertTrue(any(d.name == '计算机科学与工程学院（网络空间安全学院）' for d in units))
        self.assertFalse(next(d.list_url for d in units if d.name == '电子信息智能研究院'))
        self.assertEqual(sum(len(nodes) for nodes in self.tree().values()), 43)

    def test_portable_backup_keeps_unit_type_and_does_not_merge_it_with_a_column(self):
        from backend.services.data_transfer import export_data, read_backup, merge_data
        self.publish()
        unit = Department.query.filter_by(kind='unit', name='计算机学院').one()
        source = Department(school_id=self.school.id, name=unit.name, list_url=unit.list_url)
        db.session.add(source); db.session.commit()
        with export_data() as package:
            data = read_backup(package)
        count = Department.query.count()
        merge_data(data)
        self.assertEqual(Department.query.count(), count)
        self.assertEqual(Department.query.filter_by(name=unit.name).count(), 2)
        self.assertEqual({d.kind for d in Department.query.filter_by(name=unit.name)}, {'unit', 'column'})

    def test_subscription_page_renders_the_same_structure_and_saves_whole_unit(self):
        self.publish(); self.connect(CS + 'notices/'); self.connect(LANG + 'notices/')
        client = self.fixture.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = Subscription.query.one().user_id
            session['_csrf_token'] = 'token'
        response = client.get(f'/subscriptions/{self.school.id}')
        self.assertEqual(response.status_code, 200)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'lxml')
        nodes = soup.select('.source-unit')
        self.assertEqual(len(nodes), 2)
        self.assertTrue(all(len(n.select('.source-entry')) == 1 for n in nodes))
        unit = Department.query.filter_by(kind='unit', name='计算机学院').one()
        result = client.post(f'/subscriptions/{self.school.id}', data={
            'csrf_token': 'token', 'mode': 'selected', 'department': str(unit.id)})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(Subscription.query.one().department_ids, [unit.id])
