from pathlib import Path
import sys
import tempfile
import unittest
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.source_inventory import Inventory
from backend.services.student_sources import student_priority
from backend.scraper.discovery.inventory_crawler import crawl_site

ROOT = 'https://example.edu.cn/'


class StudentSourceFocusTests(unittest.TestCase):
    def test_prioritizes_rosters_teaching_and_colleges_over_publicity(self):
        with tempfile.TemporaryDirectory() as folder:
            inv = Inventory(Path(folder) / 'inventory.db');key = inv.ensure_site('示例大学', ROOT)
            for name, kind, depth, path in [('学校新闻', 'channel', 1, []), ('教务通知', 'channel', 7, []),
                    ('计算机学院', 'unit', 2, []), ('院系设置', 'directory', 1, []),
                    ('学院动态', 'channel', 3, ['示例大学', '计算机学院']), ('采购公告', 'channel', 1, [])]:
                inv.enqueue(key, ROOT + name + '/', name, kind, depth, path, 'school_domain')
            selected = []
            while (page := inv.claim(key, focus='student')) is not None:
                selected.append(page['label'])
            self.assertEqual(selected, ['示例大学', '院系设置', '教务通知', '计算机学院', '学院动态'])
            self.assertEqual({p['label'] for p in inv.report(key)['pages'] if p['state'] == 'pending'}, {'学校新闻', '采购公告'})
            self.assertIsNotNone(inv.claim(key, focus='all'))

    def test_school_named_college_does_not_make_all_its_paths_academic(self):
        self.assertIsNone(student_priority('channel', '学校新闻', json.dumps(['某艺术学院']), '某艺术学院'))
        self.assertEqual(student_priority('channel', '学院动态', json.dumps(['某艺术学院', '音乐学院']), '某艺术学院'), 5)
        self.assertIsNone(student_priority('channel', '采购公告', json.dumps(['音乐学院']), '某艺术学院'))

    def test_student_modes_preserve_majors_and_administration_notice_categories(self):
        for label in ('专业设置', '培养方案', '选课通知', '考试安排', '学籍管理', '学位申请', '本科生院', '教务处'):
            self.assertEqual(student_priority('channel', label), 2, label)
        self.assertEqual(student_priority('channel', '通知公告'), 4)
        self.assertIsNone(student_priority('unit', '党委宣传部'))
        self.assertIsNone(student_priority('directory', '组织机构', json.dumps(['党委宣传部'])))

    def test_major_directory_entrances_are_discovered_without_inventing_major_owners(self):
        from backend.scraper.discovery.structure import extract_structure
        html = '<nav><a href="/majors/">专业设置</a><a href="/profiles/">专业介绍</a></nav>'
        parsed = extract_structure(html, ROOT, ROOT, 'root', '示例大学')
        self.assertEqual({(l['label'], l['kind'], l['decision']) for l in parsed['links']},
                         {('专业设置', 'directory', 'follow'), ('专业介绍', 'directory', 'follow')})
        self.assertFalse(any(n['kind'] == 'unit' and n['name'] != '示例大学' for n in parsed['nodes']))

    def test_student_crawl_reaches_teaching_branch_without_visiting_publicity(self):
        with tempfile.TemporaryDirectory() as folder:
            inv = Inventory(Path(folder) / 'inventory.db');key = inv.ensure_site('示例大学', ROOT)
            visited = []
            def fetch(url):
                visited.append(url)
                html = ('<a href="/news/">学校新闻</a><a href="/teaching/">教务处</a>' if url == ROOT else
                        '<title>示例大学教务处</title><a href="/course/">选课通知</a>' if url.endswith('/teaching/') else '<p>栏目内容</p>')
                return {'html': html, 'url': url, 'status': 200}
            report = crawl_site(inv, key, max_pages=10, workers=1, fetcher=fetch, focus='student')
            self.assertEqual(visited, [ROOT, ROOT + 'teaching/', ROOT + 'course/'])
            self.assertFalse(report['accepted'])
            self.assertEqual(next(p for p in report['pages'] if p['url'] == ROOT + 'news/')['state'], 'pending')
            self.assertEqual(report['discovery_focus'], 'student')

    def test_candidate_selection_uses_student_scope_without_erasing_other_feeds(self):
        from backend.scraper.discovery.inventory_crawler import inspect_page
        from backend.services.source_catalog import publication_candidates
        with tempfile.TemporaryDirectory() as folder:
            inv = Inventory(Path(folder) / 'inventory.db');key = inv.ensure_site('示例大学', ROOT)
            html = ''
            for n, heading in enumerate(('选课通知', '校园新闻', '人才招聘')):
                html += '<section><h2>' + heading + '</h2><ul>' + ''.join(
                    '<li><a href="/info/1/' + str(n * 10 + i) + '.htm">' +
                    ('学期课程选择工作安排通知' if n == 0 else '学校举行校园摄影主题展览') +
                    '</a><time>2026-09-20</time></li>' for i in (1, 2)) + '</ul></section>'
            report = inv.report(key)
            inspect_page(inv, report['site'], report['pages'][0], fetcher=lambda _: {'html': html, 'url': ROOT, 'status': 200})
            report = inv.report(key);structure = inv.structure(key)
            self.assertEqual({c['name'] for c in publication_candidates(report, structure)}, {'选课通知', '校园新闻', '人才招聘'})
            self.assertEqual([c['name'] for c in publication_candidates(report, structure, focus='student')], ['选课通知'])


if __name__ == '__main__':
    unittest.main()
