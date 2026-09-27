"""Programme lists retain their evidence without becoming publishing units."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure, publication_evidence, add_publication_structure
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_inventory import Inventory
from backend.services.source_relationships import SourceRelationships
from backend.services.student_sources import student_priority
from backend.routes.source_structure import major_directory_tree

ROOT = 'https://www.cupk.edu.cn/'
PETROLEUM = ROOT + 'sgxy/bksjy/bkzysz/'
MECHANICAL = ROOT + 'jdxy/bkjy/zysz/jxsjzzjqzdh/'
ROWS = [('石油工程专业', '/sgxy/c/2026-08-04/537603.shtml'),
        ('油气储运工程专业', '/sgxy/c/2026-07-08/535274.shtml')]


def petroleum_list():
    return '<ul class="middleArticle__articleList">' + ''.join(
        '<li class="middleArticle--articleList"><a class="middleArticle__articleList--article" href="' + u +
        '">' + n + '</a><span class="middleArticle__articleList--date">2026-08-04</span></li>'
        for n, u in ROWS) + '</ul>'


def mechanical_list(context='专业介绍'):
    rows = ''.join('<li><a href="' + url + '">' + name + '</a></li>' for name, url in [
        ('机械设计制造及其自动化', '/jdxy/bkjy/zysz/jxsjzzjqzdh/'),
        ('过程装备与控制工程', '/jdxy/bkjy/zysz/gczbykzgc/'), ('自动化', '/jdxy/zdh/')])
    return ('<ul class="middleLeft__left--nav"><li class="middleLeft__left--navClick"><a href="' +
            MECHANICAL + '">' + context + '</a></li></ul><ul class="middleTop__nav">' + rows +
            '</ul><div class="middleArticle__nav"><ul>' + rows + '</ul></div>')


class MajorDirectoryTests(unittest.TestCase):
    def parse(self, html, url=PETROLEUM):
        result = extract_structure(html, url, ROOT, 'directory', '专业介绍')
        add_publication_structure(result, publication_evidence(html, url))
        return result

    def test_dated_profile_list_is_a_directory_and_profiles_remain_followable(self):
        parsed = self.parse(petroleum_list())
        majors = [n for n in parsed['nodes'] if n['kind'] == 'major']
        self.assertEqual([n['name'] for n in majors], [n for n, _ in ROWS])
        owner = next(n for n in parsed['nodes'] if n['relation'] == 'page_identity')
        self.assertTrue(all(n['parent'] == owner['key'] for n in majors))
        self.assertTrue(all(l['decision'] == 'follow' for l in parsed['links'] if l['kind'] == 'major'))
        self.assertIsNone(publication_evidence(petroleum_list(), PETROLEUM))

    def test_same_template_on_news_page_remains_a_publication_list(self):
        self.assertIsNotNone(publication_evidence(petroleum_list(), ROOT + 'sgxy/news/'))
        self.assertFalse(any(n['kind'] == 'major' for n in self.parse(petroleum_list(), ROOT + 'sgxy/news/')['nodes']))

    def test_other_notifications_on_the_same_directory_page_remain_discoverable(self):
        notifications = '<section><h2>教学通知</h2><ul class="news-list">' + ''.join(
            '<li><a href="/info/1001/' + str(i) + '.htm">关于开展本科课程选课的通知' + str(i) +
            '</a><time>2026-09-20</time></li>' for i in range(10000, 10003)) + '</ul></section>'
        evidence = publication_evidence(petroleum_list() + notifications, PETROLEUM)
        self.assertIsNotNone(evidence)
        self.assertEqual({u for f in evidence['lists'] for u in f['article_urls']},
                         {ROOT + 'info/1001/' + str(i) + '.htm' for i in range(10000, 10003)})

    def test_two_navigation_placements_keep_the_same_three_programme_identities(self):
        majors = [n for n in self.parse(mechanical_list(), MECHANICAL)['nodes'] if n['kind'] == 'major']
        self.assertEqual(len(majors), 6)
        self.assertEqual(len({n['key'] for n in majors}), 3)
        self.assertEqual(len({n['locator'] for n in majors}), 6)
        self.assertIsNone(publication_evidence(mechanical_list(), MECHANICAL))

    def test_changed_sidebar_context_does_not_claim_programmes(self):
        self.assertFalse(any(n['kind'] == 'major' for n in self.parse(mechanical_list('学科设置'), MECHANICAL)['nodes']))

    def test_self_link_in_navigation_does_not_become_a_major_ancestor(self):
        html = ('<nav><ul><li><a href="/jdxy/bkjy/">本科教育</a><ul><li><a href="' + MECHANICAL +
                '">专业介绍</a></li></ul></li></ul></nav>') + mechanical_list()
        parsed = self.parse(html, MECHANICAL)
        records = [dict(n, node_key=n['key'], parent_key=n['parent']) for n in parsed['nodes']]
        tree = major_directory_tree(records, {})
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0]['name'], '专业介绍')
        self.assertEqual(len(tree[0]['children']), 3)
        self.assertTrue(all(n['kind'] == 'major' and not n['cycle'] for n in tree[0]['children']))

    def test_major_priority_does_not_need_a_college_word_in_the_name(self):
        self.assertEqual(student_priority('major', '自动化'), 3)
        self.assertEqual(student_priority('major', '人力资源管理'), 3)

    def test_crawl_and_college_path_use_explicit_roster_and_homepage_link(self):
        with tempfile.TemporaryDirectory() as directory:
            inv = Inventory(Path(directory) / 'sources.db')
            key = inv.ensure_site('中国石油大学（北京）克拉玛依校区', ROOT)
            roster, college = ROOT + 'units/', ROOT + 'sgxy/'
            def read(url, label, kind, html):
                inv.enqueue(key, url, label, kind, 1, [], 'school_domain')
                report = inv.report(key)
                page = next(p for p in report['pages'] if p['url'] == url)
                inspect_page(inv, report['site'], page, fetcher=lambda _: {'url': url, 'status': 200, 'html': html})
            read(roster, '院系设置', 'directory', '<a href="' + college + '">石油工程学院</a>')
            read(college, '石油工程学院', 'unit', '<title>石油工程学院</title><nav><a href="' + PETROLEUM + '">本科专业设置</a></nav>')
            read(PETROLEUM, '本科专业设置', 'directory', petroleum_list())
            report = inv.report(key)
            paths = SourceRelationships(report, inv.structure(key)).paths_for(PETROLEUM)
            self.assertEqual([p['unit_name'] for p in paths], ['石油工程学院'])
            self.assertEqual([r['url'] for r in paths[0]['references']], [roster, college])
            profiles = [p for p in report['pages'] if p['kind'] == 'major']
            self.assertEqual(len(profiles), 2)
            self.assertTrue(all(p['state'] == 'pending' for p in profiles))
            self.assertTrue(all(not SourceRelationships(report, inv.structure(key)).paths_for(p['url']) for p in profiles))
            # Removing the actual homepage link must remove the connection even
            # though the directory URL still begins with the college URL.
            read(college, '石油工程学院', 'unit', '<title>石油工程学院</title><p>本科专业设置</p>')
            self.assertEqual(SourceRelationships(inv.report(key), inv.structure(key)).paths_for(PETROLEUM), [])


if __name__ == '__main__':
    unittest.main()
