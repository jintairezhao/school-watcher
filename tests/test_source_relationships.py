"""Source-to-unit joins require current roster and exact website-entry evidence."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.source_inventory import Inventory
from backend.services.source_relationships import SourceRelationships
from backend.scraper.discovery.inventory_crawler import inspect_page

ROOT = 'https://www.example.edu.cn/'
ROSTER = ROOT + 'units/'
UNIT = ROOT + 'energy/'
NOTICES = ROOT + 'shared-notices/'


class SourceRelationshipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inventory = Inventory(Path(self.temp.name) / 'sources.db')
        self.key = self.inventory.ensure_site('某大学', ROOT)

    def page(self, url, label, kind, html, final=None):
        if kind == 'unit' and '<title>' not in html:
            html = '<title>某大学' + label + '</title>' + html
        self.inventory.enqueue(self.key, url, label, kind, 1, [], 'school_domain')
        report = self.inventory.report(self.key)
        page = next(p for p in report['pages'] if p['url'] == url)
        inspect_page(self.inventory, report['site'], page,
                     fetcher=lambda _: {'url': final or url, 'status': 200, 'html': html})

    def roster(self, rows=None):
        rows = rows or '<tr><td>科学学院</td><td><a href="' + UNIT + '">能源-材料研究中心</a></td></tr>'
        self.page(ROSTER, '院系设置', 'directory',
                  '<table><tr><th>学院</th><th>下属单位</th></tr>' + rows + '</table>')

    def unit(self, url=UNIT):
        self.page(url, '能源-材料研究中心', 'unit', '<a href="' + NOTICES + '">通知公告</a>')

    def paths(self, url=NOTICES):
        return SourceRelationships(self.inventory.report(self.key),
                                   self.inventory.structure(self.key)).paths_for(url)

    def test_exact_official_roster_and_unit_link_preserve_full_hierarchy_and_evidence(self):
        self.roster()
        self.unit()
        paths = self.paths()
        self.assertEqual(len(paths), 1)
        self.assertEqual([n['name'] for n in paths[0]['nodes']], ['院系设置', '科学学院', '能源-材料研究中心'])
        self.assertEqual(paths[0]['nodes'][-1]['relation'], 'sub_unit')
        self.assertEqual([r['url'] for r in paths[0]['references']], [ROSTER, UNIT])
        self.assertTrue(all(r['content_hash'] and r['locators'] for r in paths[0]['references']))
        self.assertFalse(paths[0]['verified'])

    def test_names_url_prefixes_and_discovery_paths_do_not_create_a_unit_relation(self):
        self.roster()
        self.page(UNIT + 'news/', '能源-材料研究中心', 'unit', '<a href="' + NOTICES + '">通知公告</a>')
        self.assertEqual(self.paths(), [])
        self.assertEqual(self.paths(UNIT + 'notices/'), [])

    def test_observed_teaching_page_preserves_college_for_its_notice_link(self):
        self.roster()
        teaching, notices = UNIT + 'teaching/', UNIT + 'teaching/notices/'
        self.page(UNIT, '能源-材料研究中心', 'unit', '<a href="' + teaching + '">本科生培养</a>')
        self.page(teaching, '本科生培养', 'channel', '<a href="' + notices + '">通知公告</a>')
        paths = self.paths(notices)
        self.assertEqual([p['unit_name'] for p in paths], ['能源-材料研究中心'])
        self.assertEqual({r['url'] for r in paths[0]['references']}, {ROSTER, UNIT, teaching})
        self.assertIn('本科生培养', [n['name'] for n in paths[0]['entry_nodes']])

    def test_side_site_navigation_cannot_propagate_college_ownership(self):
        self.roster()
        other = ROOT + 'other-site/'
        self.page(UNIT, '能源-材料研究中心', 'unit', '<a href="' + other + '">招生信息</a>')
        self.page(other, '招生信息', 'channel', '<a href="' + other + 'news/">通知公告</a>')
        self.assertEqual(self.paths(other + 'news/'), [])

    def test_college_link_to_central_feed_does_not_assign_its_articles_to_college(self):
        from backend.services.source_catalog import publication_candidates
        self.roster()
        self.unit()
        self.page(NOTICES, '通知公告', 'channel', '<section><h2>通知公告</h2><ul>' + ''.join(
            '<li><a href="/info/1/' + str(n) + '.htm">全校研究生招生通知' + str(n) +
            '</a><time>2026-09-18</time></li>' for n in range(10000, 10003)) + '</ul></section>')
        candidates = publication_candidates(self.inventory.report(self.key), self.inventory.structure(self.key))
        self.assertTrue(candidates)
        self.assertTrue(all(c['group_name'] == '' for c in candidates))
        self.assertTrue(candidates[0]['source_structure'])

    def test_footer_news_link_is_not_evidence_of_the_unit_publishing_that_column(self):
        self.roster()
        self.page(UNIT, '能源-材料研究中心', 'unit', '<div class="footer"><a href="' + NOTICES + '">学校新闻</a></div>')
        self.assertEqual(self.paths(), [])
        row = next(n for n in self.inventory.structure(self.key) if n['reference_url'] == UNIT and n['name'] == '学校新闻')
        self.assertEqual(row['relation'], 'footer_link')

    def test_news_card_link_to_another_unit_homepage_does_not_claim_its_publications(self):
        other = ROOT + 'geoscience/'
        self.roster('<tr><td>科学学院</td><td><a href="' + UNIT + '">能源-材料研究中心</a></td></tr>'
                    '<tr><td>工程学院</td><td><a href="' + other + '">地球科学与工程学院</a></td></tr>')
        self.page(UNIT, '能源-材料研究中心', 'unit',
                  '<main><div class="news-card"><a href="' + other + '">'
                  '2025年自治区重点实验室第一届学术委员会会议</a></div></main>')
        self.page(other, '地球科学与工程学院', 'unit', '<section><h2>学院新闻</h2><ul>' + ''.join(
                  '<li><a href="/info/1/' + str(n) + '.htm">学院开展地质科学研究交流活动' + str(n) +
                  '</a><time>2026-09-18</time></li>' for n in range(10000, 10003)) + '</ul></section>')
        report = self.inventory.report(self.key)
        self.assertTrue(next(p for p in report['pages'] if p['url'] == other)['feed_json'])
        self.assertEqual([p['unit_name'] for p in self.paths(other)], ['地球科学与工程学院'])

    def test_one_college_listed_at_two_campuses_keeps_both_location_paths(self):
        root = 'https://www.sdu.edu.cn/'
        self.key = self.inventory.ensure_site('某大学', root)
        unit, channel = root + 'materials/', root + 'materials/notices/'
        html = '<div class="nygljg"><div class="wp">' + ''.join(
            '<dl><dt>' + campus + ' 济南市某路1号 邮编：250000</dt><dd><ul><li><h4><a href="' + unit +
            '">材料科学与工程学院</a></h4></li></ul></dd></dl>' for campus in ('千佛山校区', '兴隆山校区')) + '</div></div>'
        self.page(root + 'zzjg/xysz.htm', '学院设置', 'directory', html)
        self.page(unit, '材料科学与工程学院', 'unit', '<nav><a href="' + channel + '">通知公告</a></nav>')
        paths = self.paths(channel)
        self.assertEqual(len(paths), 2)
        self.assertEqual({p['nodes'][-2]['name'] for p in paths}, {'千佛山校区', '兴隆山校区'})
        self.assertTrue(all(p['nodes'][-2]['relation'] == 'campus_group' for p in paths))
        self.assertTrue(all(p['nodes'][-1]['relation'] == 'campus_directory_entry' for p in paths))

    def test_div_based_official_navigation_preserves_its_groups(self):
        self.roster()
        self.page(UNIT, '能源-材料研究中心', 'unit', '<header><div class="c-header_lib_s">'
                  '<a href="/teaching/">本科生教育</a><ul><li><a href="' + NOTICES +
                  '">教学通知</a></li></ul></div></header>')
        paths = self.paths()
        self.assertEqual(len(paths), 1)
        self.assertEqual([n['name'] for n in paths[0]['entry_nodes']], ['本科生教育'])
        self.assertEqual(paths[0]['entry_nodes'][0]['relation'], 'menu_group')
        self.assertEqual(paths[0]['entry_names'], ['教学通知'])

    def test_non_keyword_navigation_becomes_a_column_path_only_with_publication_evidence(self):
        self.roster()
        self.page(UNIT, '能源-材料研究中心', 'unit', '<header><div class="c-header_lib_s">'
                  '<a href="/union/">工会活动</a><ul><li><a href="' + NOTICES +
                  '">健康小知识</a></li></ul></div></header>')
        self.assertEqual(self.paths(), [])
        self.page(NOTICES, '健康小知识', 'navigation', '<section><h2>健康小知识</h2><ul>' + ''.join(
                  '<li><a href="/info/1/' + str(n) + '.htm">秋季校园健康知识普及活动' + str(n) +
                  '</a><time>2026-09-18</time></li>' for n in range(10000, 10003)) + '</ul></section>')
        paths = self.paths()
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0]['entry_names'], ['健康小知识'])
        self.assertEqual([n['name'] for n in paths[0]['entry_nodes']], ['工会活动'])

    def test_unit_under_its_website_menu_keeps_navigation_parent_without_claiming_subordination(self):
        self.page(UNIT, '能源-材料研究中心', 'unit', '<header><div class="c-header_lib_s">'
                  '<a href="/organization/">组织机构</a><ul><li><a href="/office/">学院办公室</a>'
                  '</li></ul></div></header>')
        nodes = self.inventory.structure(self.key)
        office = next(n for n in nodes if n['name'] == '学院办公室')
        parent = next(n for n in nodes if n['node_key'] == office['parent_key'])
        self.assertEqual(parent['name'], '组织机构')
        self.assertEqual(office['relation'], 'navigation_entry')

    def test_all_roster_placements_and_all_units_linking_one_column_are_retained(self):
        self.roster(''.join('<tr><td>' + parent + '</td><td><a href="' + UNIT + '">能源-材料研究中心</a></td></tr>'
                            for parent in ('科学学院', '工程学院')) +
                    '<tr><td>医学部</td><td><a href="' + ROOT + 'hospital/">附属医院</a></td></tr>')
        self.unit()
        self.page(ROOT + 'hospital/', '附属医院', 'unit', '<a href="' + NOTICES + '">通知公告</a>')
        paths = self.paths()
        self.assertEqual(len(paths), 3)
        self.assertEqual({tuple(n['name'] for n in p['nodes']) for p in paths}, {
            ('院系设置', '科学学院', '能源-材料研究中心'),
            ('院系设置', '工程学院', '能源-材料研究中心'), ('院系设置', '医学部', '附属医院')})

    def test_shared_table_row_stays_a_group_without_choosing_one_parent_unit(self):
        self.roster('<tr><td><p>组织人事部</p><p>党委教师工作部</p></td><td><a href="' + UNIT + '">师资办公室</a></td></tr>')
        self.page(UNIT, '师资办公室', 'unit', '<a href="' + NOTICES + '">通知公告</a>')
        nodes = self.paths()[0]['nodes']
        self.assertEqual(nodes[-2]['kind'], 'group')
        self.assertEqual(nodes[-2]['relation'], 'shared_table_row')
        self.assertEqual(nodes[-2]['name'], '组织人事部 / 党委教师工作部')

    def test_unit_profile_reusing_a_parent_college_header_does_not_claim_all_college_columns(self):
        self.roster()
        self.page(UNIT, '能源-材料研究中心', 'unit', '<title>某大学科学学院</title><header>'
                  '<a href="' + NOTICES + '">通知公告</a></header><article>能源-材料研究中心介绍</article>')
        self.assertEqual(self.paths(), [])

    def test_exact_header_identity_connects_student_column_when_title_is_generic(self):
        self.roster()
        self.page(UNIT, '能源-材料研究中心', 'unit', '<title>首页</title><header><div class="logo">'
                  '<img alt="某大学能源-材料研究中心" src="logo.png"></div>'
                  '<a href="' + NOTICES + '">教学通知</a></header>')
        paths = self.paths()
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0]['website_identity']['source'], 'header_logo_alt')
        self.assertEqual(paths[0]['unit_name'], '能源-材料研究中心')
        proof = next(r for r in paths[0]['references'] if r['url'] == UNIT)
        self.assertTrue(any('img' in loc for loc in proof['locators']))
        self.assertEqual([n['name'] for n in paths[0]['nodes']], ['院系设置', '科学学院', '能源-材料研究中心'])

    def test_specific_unit_groups_sources_even_when_school_home_links_to_its_website(self):
        from backend.services.source_catalog import publication_candidates
        self.roster()
        self.page(ROOT, '某大学', 'root', '<nav><a href="' + UNIT + '">能源-材料研究中心</a></nav>')
        self.page(UNIT, '能源-材料研究中心', 'unit', '<title>首页</title><header><img alt="某大学能源-材料研究中心"></header>'
                  '<section><h2>教学通知</h2><ul>' + ''.join('<li><a href="/info/1/' + str(i) +
                  '.htm">本学期选课调整通知</a><time>2026-09-20</time></li>' for i in (1, 2)) + '</ul></section>')
        result = publication_candidates(self.inventory.report(self.key), self.inventory.structure(self.key))
        candidates = [c for c in result if c['list_url'] == UNIT]
        self.assertTrue(candidates)
        self.assertTrue(all(c['group_name'] == '能源-材料研究中心' for c in candidates))
        self.assertTrue(any(p['basis'] == 'school_website_entry' for p in candidates[0]['source_structure']))

    def test_body_mention_or_parent_logo_does_not_assign_childs_columns(self):
        self.roster()
        for brand in ['<main><img alt="某大学能源-材料研究中心"></main>',
                      '<header><img alt="某大学科学学院"></header>',
                      '<footer><img alt="某大学能源-材料研究中心"></footer>']:
            self.page(UNIT, '能源-材料研究中心', 'unit', '<title>首页</title>' + brand +
                      '<a href="' + NOTICES + '">教学通知</a>')
            self.assertEqual(self.paths(), [])

    def test_stale_branding_metadata_cannot_establish_identity(self):
        from backend.services.source_ownership import BRANDING_PREFIX
        self.roster()
        self.page(UNIT, '能源-材料研究中心', 'unit', '<title>首页</title><meta property="og:site_name" '
                  'content="某大学能源-材料研究中心"><a href="' + NOTICES + '">教学通知</a>')
        self.assertEqual(len(self.paths()), 1)
        with self.inventory.connect() as c:
            row = c.execute('SELECT notes_json FROM pages WHERE site_key=? AND url=?', (self.key, UNIT)).fetchone()
            notes = json.loads(row['notes_json'])
            for i, note in enumerate(notes):
                if note.startswith(BRANDING_PREFIX):
                    evidence = json.loads(note[len(BRANDING_PREFIX):]);evidence['content_hash'] = 'old-snapshot'
                    notes[i] = BRANDING_PREFIX + json.dumps(evidence)
            c.execute('UPDATE pages SET notes_json=? WHERE site_key=? AND url=?', (json.dumps(notes), self.key, UNIT))
        self.assertEqual(self.paths(), [])

    def test_http_success_with_official_template_error_still_requires_review(self):
        from backend.routes.source_structure import page_health_label
        self.page(NOTICES, '健康小知识', 'navigation', '<p>header.template.html 第48行发生错误: '
                  '未指定栏目或指定的栏目不存在</p>')
        page = next(p for p in self.inventory.report(self.key)['pages'] if p['url'] == NOTICES)
        self.assertEqual(page['state'], 'fetched')
        self.assertIn('official_template_error_requires_review', json.loads(page['notes_json']))
        self.assertEqual(page_health_label(page), '官网页面存在模板错误')
        self.assertIsNotNone(self.inventory.snapshot(self.key, NOTICES))

    def test_changed_or_unavailable_reference_invalidates_the_join(self):
        self.roster()
        self.unit()
        self.inventory.finish(self.key, ROSTER, html='<p>机构调整中</p>', state='fetched')
        self.assertEqual(self.paths(), [])
        self.roster()
        self.inventory.finish(self.key, UNIT, state='failed', health='unreachable')
        self.assertEqual(self.paths(), [])

    def test_observed_redirect_is_an_alias_but_http_and_https_are_not_assumed_equal(self):
        self.roster()
        self.unit(UNIT.replace('https:', 'http:'))
        self.assertEqual(self.paths(), [])
        self.page(UNIT, '能源-材料研究中心', 'unit', '<a href="' + NOTICES + '">通知公告</a>', final=ROOT + 'new-energy/')
        self.assertEqual(len(self.paths()), 1)

    def test_friendly_links_and_external_references_do_not_establish_roster_membership(self):
        self.page(ROOT, '某大学', 'root', '<footer><a href="' + UNIT + '">能源-材料研究中心</a></footer>')
        self.unit()
        self.assertEqual(self.paths(), [])
        self.roster()
        self.page(UNIT, '能源-材料研究中心', 'unit', '<a href="https://partner.example.org/news/">通知公告</a>')
        self.assertEqual(self.paths('https://partner.example.org/news/'), [])

    def test_cyclic_or_missing_parents_do_not_fabricate_a_complete_path(self):
        self.roster()
        self.unit()
        with self.inventory.connect() as conn:
            conn.execute("UPDATE structure SET parent_key=node_key WHERE site_key=? AND reference_url=? AND name=?",
                         (self.key, ROSTER, '科学学院'))
        self.assertEqual(self.paths(), [])

    def test_subscription_keeps_one_choice_and_all_paths_without_changing_saved_selection(self):
        from backend import create_app
        from backend.database.db import db
        from backend.database.models import School, Department, Subscription, User
        from bs4 import BeautifulSoup
        self.roster(''.join('<tr><td>' + parent + '</td><td><a href="' + UNIT + '">能源-材料研究中心</a></td></tr>'
                            for parent in ('科学学院', '工程学院')))
        self.unit()
        app = create_app({'TESTING': True, 'SECRET_KEY': 'source-links', 'SQLALCHEMY_DATABASE_URI': 'sqlite://',
                          'SOURCE_INVENTORY_PATH': self.inventory.path})
        with app.app_context():
            db.create_all()
            user, school = User(username='reader', password_hash='unused'), School(name='某大学', url=ROOT)
            db.session.add_all([user, school]); db.session.flush()
            source = Department(school_id=school.id, name='原有栏目名称', group_name='原有分组', list_url=NOTICES)
            unknown = Department(school_id=school.id, name='未知栏目', list_url=ROOT + 'unknown/')
            db.session.add_all([source, unknown]); db.session.flush()
            subscription = Subscription(user_id=user.id, school_id=school.id, department_ids=[source.id])
            db.session.add(subscription); db.session.commit()
            client = app.test_client()
            with client.session_transaction() as session:
                session['user_id'] = user.id
                session['_csrf_token'] = 'source-links'
            response = client.get(f'/subscriptions/{school.id}')
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.text, 'lxml')
            choices = soup.select(f'input[name="department"][value="{source.id}"]')
            self.assertEqual(len(choices), 1)
            self.assertTrue(choices[0].has_attr('checked'))
            self.assertEqual(len(soup.select('.source-path')), 2)
            self.assertIn('科学学院', response.text)
            self.assertIn('工程学院', response.text)
            self.assertIn('单位与栏目关系待核实', response.text)
            self.assertEqual(subscription.department_ids, [source.id])
            self.assertEqual((source.name, source.group_name), ('原有栏目名称', '原有分组'))
            # Existing selection actions continue to use the unchanged column ID.
            saved = client.post(f'/subscriptions/{school.id}', data={'csrf_token': 'source-links',
                                'mode': 'selected', 'department': str(unknown.id)})
            self.assertEqual(saved.status_code, 302)
            self.assertEqual(subscription.department_ids, [unknown.id])
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def test_discovery_path_alone_no_longer_becomes_a_source_group(self):
        from backend.services.source_catalog import publication_candidates
        self.page(UNIT, '能源-材料研究中心', 'unit', '<p>页面</p>')
        with self.inventory.connect() as conn:
            conn.execute('UPDATE pages SET path_json=?,feed_json=? WHERE site_key=? AND url=?',
                         (json.dumps(['友好链接', '猜测的院系']), json.dumps({'name': '通知公告', 'samples': []}), self.key, UNIT))
        report = self.inventory.report(self.key)
        candidate = publication_candidates(report, self.inventory.structure(self.key))[0]
        self.assertEqual(candidate['group_name'], '')
        self.assertEqual(candidate['source_structure'], [])
        self.assertEqual(candidate['discovery_path'], ['友好链接', '猜测的院系'])
        self.roster()
        candidate = publication_candidates(self.inventory.report(self.key), self.inventory.structure(self.key))[0]
        self.assertEqual(candidate['group_name'], '能源-材料研究中心')
        self.assertEqual(candidate['source_structure'][0]['nodes'][-2]['name'], '科学学院')


if __name__ == '__main__':
    unittest.main()
