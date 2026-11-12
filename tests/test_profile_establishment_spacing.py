"""Inline HTML formatting must not hide an explicit profile opening."""
import unittest

from bs4 import BeautifulSoup
from backend.services.source_ownership import profile_identity_candidates


class ProfileEstablishmentSpacingTests(unittest.TestCase):
    def test_formatted_year_keeps_literal_subject_and_original_text(self):
        for gap in (' ', '\n', '\u00a0'):
            html = ('<title>学院简介</title><div class="wp_articlecontent"><p>'
                    '<span>东北大学材料科学与工程学院成立于</span>'
                    +gap+'<span>2015</span>年12月，依托材料科学与工程一级学科建立，'
                    '开展人才培养和科学研究工作。</p></div>')
            soup = BeautifulSoup(html, 'lxml')
            with self.subTest(gap=repr(gap)):
                result = profile_identity_candidates(soup)
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]['entity_name'], '东北大学材料科学与工程学院')
                self.assertEqual(result[0]['identity'], soup.p.get_text(' ', strip=True))
                self.assertEqual(result[0]['source'], 'unit_profile_self_description')

    def test_spacing_does_not_supply_missing_year_or_promote_later_mentions(self):
        for paragraphs in (
            '<p>示例学院成立于 201年，依托本校相关学科建设，开展人才培养及科学研究工作。</p>',
            '<p>本页记载近期学院新闻和其他工作情况。</p>'
            '<p>示例学院成立于 2015年，依托本校相关学科建设，开展人才培养及科学研究工作。</p>',
        ):
            with self.subTest(paragraphs=paragraphs):
                soup = BeautifulSoup('<div class="wp_articlecontent">'+paragraphs+'</div>', 'lxml')
                self.assertEqual(profile_identity_candidates(soup), [])


if __name__ == '__main__':
    unittest.main()
