"""Channel presentation uses real source evidence and keeps homonyms distinct."""
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry, School
from backend.services.source_channels import channels_for
from backend.services.source_inventory import site_key
from backend.services.source_ownership import BRANDING_PREFIX

ROOT = 'https://example.edu.cn/'


class SourceChannelTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()
        self.school = db.session.get(School, self.fixture.school_id)
        self.catalog_patch = patch('backend.services.source_channels.RuntimeCatalog')
        self.catalog = self.catalog_patch.start().return_value
        self.catalog.report.side_effect = AssertionError('Do not load the entire investigation')
        self.catalog.snapshot.side_effect = AssertionError('Do not load HTML')
        self.catalog.paths.return_value = {}
        self.pages = {}
        self.catalog.page_metadata.side_effect = lambda key, urls: {url: self.pages[url] for url in urls if url in self.pages}

    def tearDown(self):
        self.catalog_patch.stop()
        self.fixture.tearDown()

    def source(self, path, name='通知公告', kind='column', group=''):
        source = Department(school_id=self.school.id, name=name, kind=kind,
            list_url=path if path.startswith('http') else ROOT + path,
            list_selector='li' if kind == 'column' else None, group_name=group)
        db.session.add(source); db.session.commit()
        return source

    def link(self, parent, source):
        db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=source.id, position=0))
        db.session.commit()

    def page(self, url, *, label='', title='通知公告', kind='channel', stale=False):
        page = {'url': url, 'final_url': url, 'state': 'fetched', 'kind': kind,
                'title': title, 'content_hash': 'current', 'notes_json': '[]'}
        if label:
            page['notes_json'] = json.dumps([BRANDING_PREFIX + json.dumps({
                'site_key': site_key(ROOT), 'reference_url': url, 'final_url': url,
                'content_hash': 'old' if stale else 'current', 'candidates': [{
                    'source': 'og:site_name', 'identity': label, 'locator': 'meta[property="og:site_name"]'}]})])
        return page

    def path(self, name, url):
        return {'basis': 'official_website_entry', 'nodes': [
            {'kind': 'unit', 'name': name, 'url': url, 'node_key': url}], 'references': []}

    def test_same_named_columns_have_distinct_real_publishers_and_shared_unit_channels(self):
        cs = self.source('cs/', '计算机学院', 'unit', '院系设置')
        art = self.source('art/', '艺术学院', 'unit', '院系设置')
        notices = self.source('cs/notices/')
        news = self.source('cs/news/', '计算机学院-新闻动态')
        other = self.source('art/notices/')
        for parent, source in ((cs, notices), (cs, news), (art, other)):
            self.link(parent, source)
        info = channels_for([notices, news, other])
        self.assertEqual(info[notices.id]['breadcrumb'], '测试大学 / 计算机学院 / 通知公告')
        self.assertEqual(info[news.id]['column_label'], '新闻动态')
        self.assertEqual(info[notices.id]['publisher_key'], info[news.id]['publisher_key'])
        self.assertNotEqual(info[notices.id]['publisher_key'], info[other.id]['publisher_key'])
        self.catalog.report.assert_not_called()

    def test_same_named_units_with_different_websites_never_merge(self):
        first = self.source('north/', '工程学院', 'unit')
        second = self.source('south/', '工程学院', 'unit')
        a, b = self.source('north/notices/'), self.source('south/notices/')
        self.link(first, a); self.link(second, b)
        info = channels_for([a, b])
        self.assertNotEqual(info[a.id]['publisher_key'], info[b.id]['publisher_key'])
        self.assertEqual(info[a.id]['official_url'], a.list_url)
        self.assertEqual(info[a.id]['publisher_label'], '工程学院（example.edu.cn/north）')
        self.assertIn('工程学院（example.edu.cn/south）', info[b.id]['breadcrumb'])
        self.assertEqual(channels_for([a])[a.id]['publisher_label'], info[a.id]['publisher_label'])

    def test_navigation_group_alone_is_not_a_publisher_or_a_review_request(self):
        group = self.source('colleges/', '院系设置', 'group')
        source = self.source('x/notices/', group='院系设置')
        self.link(group, source)
        info = channels_for([source])[source.id]
        self.assertEqual(info['basis'], 'official_page')
        self.assertEqual(info['publisher_label'], 'example.edu.cn/x/notices')
        self.assertIn('院系设置 / 通知公告', info['breadcrumb'])
        self.assertEqual(info['column_label'], '院系设置 / 通知公告')
        self.assertNotIn('核实', str(info))

    def test_official_path_needs_no_complete_roster_and_matches_persisted_unit_key(self):
        unit = self.source('cs/', '计算机学院', 'unit')
        first, second = self.source('cs/notices/'), self.source('cs/news/', '新闻动态')
        self.link(unit, first)
        self.catalog.paths.return_value = {second.list_url: [self.path(unit.name, unit.list_url)]}
        info = channels_for([first, second])
        self.assertEqual(info[second.id]['basis'], 'official_path')
        self.assertEqual(info[first.id]['publisher_key'], info[second.id]['publisher_key'])
        self.catalog.page_metadata.assert_not_called()

    def test_cross_link_and_unconfirmed_path_cannot_supply_publisher(self):
        source = self.source('https://partner.invalid/notices/')
        self.catalog.paths.return_value = {source.list_url: [self.path('计算机学院', ROOT + 'cs/')]}
        info = channels_for([source])[source.id]
        self.assertEqual(info['basis'], 'official_page')
        self.assertEqual(info['publisher_label'], 'partner.invalid/notices')
        source.list_url = ROOT + 'cs/notices/'
        self.catalog.paths.return_value = {source.list_url: [dict(self.path('计算机学院', ROOT + 'cs/'),
            basis='official_directory_navigation', identity_pending=True)]}
        self.assertEqual(channels_for([source])[source.id]['basis'], 'official_page')

    def test_explicit_site_identity_unifies_columns_only_through_observed_entrance(self):
        notices, news = self.source('cs/notices/'), self.source('cs/news/', '新闻动态')
        pages = [
            self.page(ROOT + 'cs/', label='测试大学计算机学院', kind='unit'),
            self.page(notices.list_url, label='测试大学计算机学院'),
            self.page(news.list_url, label='测试大学计算机学院')]
        self.pages = {page['url']: page for page in pages}
        info = channels_for([notices, news])
        self.assertEqual(info[notices.id]['basis'], 'site_identity')
        self.assertEqual(info[notices.id]['publisher_label'], '测试大学计算机学院')
        self.assertEqual(info[notices.id]['publisher_key'], info[news.id]['publisher_key'])

    def test_stale_branding_and_article_style_titles_do_not_invent_publishers(self):
        source = self.source('generic/notices/')
        self.pages = {source.list_url: self.page(source.list_url,
            label='工程学院', stale=True, title='关于成立创新创业学院的通知')}
        self.assertEqual(channels_for([source])[source.id]['basis'], 'official_page')
        self.pages = {source.list_url: self.page(source.list_url,
            title='通知公告 - 学生工作处 - 测试大学')}
        self.assertEqual(channels_for([source])[source.id]['publisher_label'], '学生工作处')

    def test_unidentified_channels_keep_distinct_query_and_fragment_routes(self):
        first = self.source('portal?unit=one#/notice')
        second = self.source('portal?unit=two#/notice')
        info = channels_for([first, second])
        self.assertNotEqual(info[first.id]['publisher_key'], info[second.id]['publisher_key'])
        self.assertNotEqual(info[first.id]['breadcrumb'], info[second.id]['breadcrumb'])

    def test_batch_request_reads_published_catalogue_once_and_never_fetches(self):
        first, second = self.source('one/notices/'), self.source('two/notices/')
        with self.fixture.app.test_request_context('/'), \
             patch('backend.scraper.discovery.inventory_crawler.fetch_page', side_effect=AssertionError('No network')), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No AI')):
            before = channels_for([first])
            after = channels_for([first, second])
            self.assertEqual(before[first.id], after[first.id])
            after[first.id]['publisher_label'] = 'Caller mutation'
            self.assertNotEqual(channels_for([first])[first.id]['publisher_label'], 'Caller mutation')
        self.catalog.report.assert_not_called()
        self.catalog.snapshot.assert_not_called()
        self.assertEqual(self.catalog.page_metadata.call_count, 2)
        requested = [url for call in self.catalog.page_metadata.call_args_list for url in call.args[1]]
        self.assertEqual(len(requested), len(set(requested)))
        self.assertLessEqual(len(requested), 6)
        self.assertEqual(self.catalog.paths.call_count, 1)

    def test_legacy_group_is_column_context_without_claiming_publisher(self):
        source = self.source('https://www.tsinghua.edu.cn/news/', '院内新闻', group='地球科学与工程学院')
        info = channels_for([source])[source.id]
        self.assertEqual(info['publisher_label'], 'www.tsinghua.edu.cn/news')
        self.assertEqual(info['column_label'], '地球科学与工程学院 / 院内新闻')
        self.assertIn('地球科学与工程学院 / 院内新闻', info['breadcrumb'])

    def test_school_site_branding_keeps_legacy_column_path_context(self):
        source = self.source('', '院内新闻', group='地球科学与工程学院')
        self.pages = {source.list_url: self.page(source.list_url, title='测试大学', kind='root')}
        info = channels_for([source])[source.id]
        self.assertEqual(info['publisher_label'], '测试大学')
        self.assertEqual(info['column_label'], '地球科学与工程学院 / 院内新闻')

    def test_school_homepage_cannot_prove_a_units_ownership_of_school_news(self):
        source = self.source('notices/')
        self.catalog.paths.return_value = {source.list_url: [self.path('工程学院', ROOT)]}
        self.assertEqual(channels_for([source])[source.id]['basis'], 'official_page')

    def test_invalid_saved_urls_are_unlinked_and_do_not_break_scope_lookup(self):
        source = self.source('https://example.edu.cn:invalid/notices/')
        self.catalog.paths.return_value = {source.list_url: [self.path('工程学院', ROOT + 'cs/')]}
        for address in ('https://example.edu.cn:invalid/notices/', 'javascript:alert(1)', 'http://127.0.0.1/',
                        'https://example.edu.cn:99999/notices/', 'https://example.edu.cn/\x00bad'):
            with self.subTest(address=address):
                source.list_url = address
                info = channels_for([source])[source.id]
                self.assertEqual(info['official_url'], '')
                self.assertEqual(info['basis'], 'official_page')
        self.catalog.page_metadata.assert_not_called()

    def test_unrequested_same_name_site_cannot_influence_publication_identity(self):
        source = self.source('cs/notices/')
        self.pages = {source.list_url: self.page(source.list_url, label='工程学院'),
            ROOT + 'unrelated/': self.page(ROOT + 'unrelated/', label='工程学院', kind='unit')}
        info = channels_for([source])[source.id]
        self.assertEqual(info['basis'], 'site_identity')
        requested = self.catalog.page_metadata.call_args.args[1]
        self.assertNotIn(ROOT + 'unrelated/', requested)
        self.assertIn(source.list_url, requested)


if __name__ == '__main__':
    unittest.main()
