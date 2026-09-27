"""Academic roster aliases, real grouped cards and imperfect official links."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import classify, extract_structure


class AcademicDirectoryTests(unittest.TestCase):
    def parse(self, school):
        root = 'https://www.' + school + '.edu.cn/'
        path = 'zzjg/xbxy.htm' if school == 'hubu' else 'zzjg/jxhkydw1.htm'
        html = (Path(__file__).parent / 'fixtures' / (school + '_academic_directory.html')).read_text(encoding='utf-8')
        # Simulate the incorrect type in the old inventory, and a fresh discovery.
        return extract_structure(html, root + path, root, 'unit', '院系入口')

    def test_directory_aliases_do_not_create_phantom_colleges(self):
        for name in ('二级学院', '教学学院', '教学院', '学部学院', '教学科研内设机构', '教学和科研单位'):
            self.assertEqual(classify(name), 'directory', name)
        for name in ('国际教学学院', '生命科学学部', '创新创业学院'):
            self.assertEqual(classify(name), 'unit', name)

    def test_hubu_groups_are_not_units_or_inferred_administrative_owners(self):
        parsed = self.parse('hubu')
        nodes = {n['name']: n for n in parsed['nodes']}
        units = [n for n in parsed['nodes'] if n['kind'] == 'unit']
        self.assertEqual(len(units), 32)
        for group in ('学部设置', '学院设置', '跨学科学院'):
            self.assertEqual(nodes[group]['kind'], 'group')
            self.assertEqual(nodes[group]['relation'], 'directory_group')
        self.assertEqual(nodes['生命科学学院']['parent'], nodes['学院设置']['key'])
        self.assertNotEqual(nodes['生命科学学院']['parent'], nodes['生命科学学部']['key'])
        for name in ('楚才学院', '创新创业学院'):
            self.assertEqual(nodes[name]['parent'], nodes['跨学科学院']['key'])
        self.assertIn('师范学院（田家炳教育学院）', nodes)

    def test_muc_sibling_cards_preserve_missing_links_and_nonindependent_labels(self):
        parsed = self.parse('muc')
        nodes = {n['name']: n for n in parsed['nodes']}
        units = [n for n in parsed['nodes'] if n['kind'] == 'unit']
        self.assertEqual(len(units), 42)
        self.assertEqual(nodes['中国语言文学学部']['parent'], nodes['文学院']['parent'])
        self.assertEqual(nodes['中华民族共同体学院']['url'], '')
        platforms = [n for n in units if n['relation'] == 'non_entity_directory_entry']
        self.assertEqual(len(platforms), 9)
        self.assertTrue(all(n['name'].startswith('（非独立科研平台）') for n in platforms))

    def test_imperfect_official_links_are_preserved_and_flagged(self):
        parsed = self.parse('muc')
        nodes = {n['name']: n for n in parsed['nodes']}
        self.assertEqual(nodes['预科教育学院']['url'], 'https://www.muc.edu.cn/zzjg/yuke.muc.edu.cn')
        self.assertEqual(nodes['铸牢中华民族共同体意识研究院']['url'], 'https://www.muc.edu.cn/zzjg/jxhkydw1.htm')
        self.assertEqual(sum(n.startswith('official_directory_link_requires_review:') for n in parsed['notes']), 3)

    def test_reviewed_directory_addresses_override_stale_unit_type(self):
        for root, path in [('https://www.shutcm.edu.cn/', 'ejxy/list.htm'),
                           ('https://www.csust.edu.cn/', 'jgsz/jxy.htm')]:
            parsed = extract_structure('<main><a href="/unit/">护理学院</a></main>', root + path, root, 'unit', '教学院')
            unit = next(n for n in parsed['nodes'] if n['name'] == '护理学院')
            self.assertEqual(unit['relation'], 'directory_entry')
            self.assertEqual(parsed['nodes'][0]['kind'], 'group')

    def test_unrelated_pages_do_not_inherit_reviewed_roster_rules(self):
        html = (Path(__file__).parent / 'fixtures' / 'hubu_academic_directory.html').read_text(encoding='utf-8')
        result = extract_structure(html, 'https://example.edu.cn/news.htm', 'https://example.edu.cn/', 'unit', '新闻页')
        self.assertFalse(any(n['relation'] == 'directory_group' for n in result['nodes']))


if __name__ == '__main__':
    unittest.main()
