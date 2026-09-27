"""Programme codes, explicit college columns, dated catalogues and joint admissions blocks."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure, publication_evidence
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_inventory import Inventory
from backend.services.source_baselines import BASELINE_DIRECTORY, check_baseline

URLS = {'snnu': 'http://www.snnu.edu.cn/jyjx/bkzy.htm',
        'hrbeu': 'http://ugs.hrbeu.edu.cn/2819/list.htm',
        'cqmu': 'https://bzkzs.cqmu.edu.cn/xxgk/xyzy.htm'}


class ProgrammeCatalogueTests(unittest.TestCase):
    def fixture(self, name):
        return (Path(__file__).parent / 'fixtures' / (name + '_programmes.html')).read_text(encoding='utf-8')

    def parse(self, name, html=None, url=None):
        return extract_structure(html or self.fixture(name), url or URLS[name], URLS[name].split('/')[0] +
                                 '//' + URLS[name].split('/')[2] + '/', 'directory', '本科专业')

    def test_explicit_college_column_builds_69_programmes_under_21_colleges(self):
        parsed = self.parse('snnu')
        nodes = {n['name']: n for n in parsed['nodes']}
        self.assertEqual(sum(n['kind'] == 'major' for n in parsed['nodes']), 69)
        self.assertEqual(sum(n['relation'] == 'programme_college' for n in parsed['nodes']), 21)
        self.assertEqual(nodes['人工智能']['parent'], nodes['计算机科学学院']['key'])
        self.assertEqual(nodes['人力资源管理']['parent'], nodes['国际商学院']['key'])
        self.assertTrue(all(not n['url'] for n in parsed['nodes'] if n['kind'] == 'major'))
        context = next(json.loads(n.split(':', 1)[1]) for n in parsed['notes'] if n.startswith('programme_catalog_context:'))
        self.assertEqual(context['period'], '2024年11月')

    def test_missing_college_column_does_not_create_owners_from_subject_categories(self):
        parsed = self.parse('hrbeu')
        majors = [n for n in parsed['nodes'] if n['kind'] == 'major']
        self.assertEqual(len(majors), 59)
        self.assertEqual(len({n['parent'] for n in majors}), 1)
        self.assertFalse(any(n['relation'] == 'programme_college' for n in parsed['nodes']))
        fields = next(json.loads(n.split(':', 1)[1]) for n in parsed['notes'] if n.startswith('programme_catalog_attributes:'))
        self.assertEqual(sum(r['fields']['专业代码'] == '080701' for r in fields), 2)
        self.assertEqual(len({n['key'] for n in majors}), 59)

    def test_joint_admissions_heading_and_repeated_programmes_keep_all_placements(self):
        parsed = self.parse('cqmu')
        majors = [n for n in parsed['nodes'] if n['kind'] == 'major']
        self.assertEqual(len(majors), 43)
        joint = next(n for n in parsed['nodes'] if n['name'] == '第一临床学院+人工智能医学学院')
        self.assertEqual(joint['kind'], 'group')
        self.assertEqual(joint['relation'], 'programme_joint_group')
        ai = [n for n in majors if n['name'] == '临床医学(AI医学创新班）']
        self.assertEqual(len(ai), 3)
        self.assertEqual(len({n['parent'] for n in ai}), 3)
        self.assertEqual(len({n['url'] for n in ai}), 3)
        self.assertTrue(any('招生网站' in n for n in parsed['notes']))

    def test_changed_headers_or_rowspans_do_not_shift_college_ownership(self):
        html = self.fixture('snnu')
        for changed in (html.replace('所在学院名称', '备注'), html.replace('<td ', '<td rowspan="2" ', 1),
                        html.replace('<tr><td ', '<tr><td rowspan="2" ', 1)):
            parsed = self.parse('snnu', changed)
            self.assertFalse(any(n['kind'] == 'major' for n in parsed['nodes']))
            self.assertTrue(any(n.startswith('programme_catalog_requires_review:') for n in parsed['notes']))

    def test_missing_admissions_block_does_not_borrow_the_next_college(self):
        html = '<div class="content_box2"><div><a href="a.htm"><div class="zszy_box_top">甲学院</div></a>'
        html += '<a href="b.htm"><div class="zszy_box_top">乙学院</div></a><div class="containerq"><div><a href="m.htm">护理学</a></div></div></div></div>'
        parsed = self.parse('cqmu', html)
        nodes = {n['name']: n for n in parsed['nodes']}
        self.assertEqual(nodes['护理学']['parent'], nodes['乙学院']['key'])
        self.assertFalse(any(n['name'] == '甲学院' and n['relation'] == 'programme_college' for n in parsed['nodes']))

    def test_professional_regions_are_not_publication_lists(self):
        for name in URLS:
            self.assertIsNone(publication_evidence(self.fixture(name), URLS[name]))
        self.assertFalse(any(n['kind'] == 'major' for n in self.parse('snnu', url='http://example.edu.cn/news/')['nodes']))

    def test_independent_baseline_checks_code_and_degree_not_just_name(self):
        with tempfile.TemporaryDirectory() as directory:
            inv = Inventory(Path(directory) / 'inventory.db')
            url = URLS['hrbeu']; root = 'http://ugs.hrbeu.edu.cn/'
            key = inv.ensure_site('哈尔滨工程大学', root)
            inv.enqueue(key, url, '专业目录', 'directory', 1, [], 'school_domain')
            report = inv.report(key); page = next(p for p in report['pages'] if p['url'] == url)
            html = self.fixture('hrbeu')
            inspect_page(inv, report['site'], page, fetcher=lambda _: {'url': url, 'status': 200, 'html': html})
            baseline = json.loads((BASELINE_DIRECTORY / 'hrbeu_undergraduate_programmes.json').read_text(encoding='utf-8'))
            baseline.update(root_url=root, reference_hash=hashlib.sha256(html.encode()).hexdigest())
            self.assertTrue(check_baseline(inv, baseline)['scope_passed'])
            baseline['entries'][0]['fields']['专业代码'] = '000000'
            result = check_baseline(inv, baseline)
            self.assertFalse(result['scope_passed'])
            self.assertEqual(result['entries'][0]['status'], 'wrong_attributes')

    def test_http_200_cms_error_is_flagged_without_treating_news_quotes_as_errors(self):
        text = '提示：访问地址无效，3027找不到对应的栏目！'
        bad = '<title>提示信息</title><div class="wp_error"><div class="wp_error_msg">' + text + '</div></div>'
        self.assertIn('official_template_error_requires_review', self.parse('snnu', bad)['notes'])
        self.assertNotIn('official_template_error_requires_review', self.parse('snnu', '<article>' + text + '</article>')['notes'])


if __name__ == '__main__':
    unittest.main()
