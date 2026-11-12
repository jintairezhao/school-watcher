"""A retained plain-text column label needs its own terminal breadcrumb proof."""
from copy import deepcopy
from pathlib import Path
import gzip
import hashlib
import unittest

from bs4 import BeautifulSoup
from backend.services.source_governance import _column_scope
from backend.services.source_scope import recover_observed_scope

URL = 'https://www.swpu.edu.cn/c_jy/xljxjy/zsxx.htm'
CONFIG = dict(name='招生信息', list_url=URL,
              list_selector='div.list-right-ul > ul > li.news-list',
              title_selector='a.list-link', link_selector='a.list-link',
              date_selector='span.sdate', content_selector='', group_name='')
WRONG = dict(container_selector='div.list-right-ul', heading_selector='div.list-right-title > p')


class DeclaredScopeLabelTests(unittest.TestCase):
    def setUp(self):
        raw = gzip.decompress((Path(__file__).parent / 'fixtures/structure/'
            'swpu-continuing-admissions-c76bb119-20261101.html.gz').read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         'c76bb1198c6e511acca62dd9ce6eb97d2baf2c683a7d2bf098527e9430a0352c')
        self.html = raw.decode('utf-8')
        self.soup = BeautifulSoup(self.html, 'lxml')

    def recover(self, *, config=None, scope=None, html=None):
        return recover_observed_scope(config or CONFIG, {'url': URL},
                                      scope or WRONG, html=html or str(self.soup))

    def test_retained_label_and_breadcrumb_correct_container_without_changing_records(self):
        before, errors, _ = _column_scope(CONFIG, {'url': URL}, WRONG, html=self.html)
        self.assertEqual(errors, ['column_identity_or_scope_unconfirmed'])
        self.assertEqual(len(before), 14)
        config, proposed = deepcopy(CONFIG), deepcopy(WRONG)
        fixed = self.recover(config=config, scope=proposed, html=self.html)
        self.assertIsNotNone(fixed)
        after, errors, region = _column_scope(config, {'url': URL}, fixed['scope'], html=self.html)
        self.assertEqual(errors, [])
        self.assertEqual(before, after)
        self.assertEqual(region['name'], '招生信息')
        self.assertEqual(config, CONFIG)
        self.assertEqual(proposed, WRONG)

    def test_plain_label_without_terminal_self_breadcrumb_is_not_evidence(self):
        self.soup.select_one('.list-right-title .local a:last-child')['href'] = 'other.htm'
        self.assertIsNone(self.recover())

    def test_missing_breadcrumb_does_not_turn_model_label_into_fact(self):
        self.soup.select_one('.list-right-title .local').decompose()
        self.assertIsNone(self.recover())

    def test_model_must_identify_the_observed_plain_label(self):
        self.assertIsNone(self.recover(scope=dict(WRONG, heading_selector='.does-not-exist')))

    def test_independent_same_named_labels_remain_ambiguous(self):
        title = self.soup.select_one('.list-right-title')
        title.p['id'] = 'first-column-label'
        title.insert_after(deepcopy(title))
        self.soup.select('.list-right-title')[1].p['id'] = 'second-column-label'
        self.assertIsNone(self.recover(scope=dict(WRONG, heading_selector='#first-column-label')))

    def test_selected_subset_does_not_establish_complete_column(self):
        self.assertIsNone(self.recover(config=dict(CONFIG,
            list_selector=CONFIG['list_selector'] + ':first-child')))

    def test_another_adjacent_column_cannot_be_absorbed(self):
        other = self.soup.new_tag('div')
        other.string = '教学管理'
        self.soup.select_one('.list-right-ul').insert_before(other)
        self.assertIsNone(self.recover())

    def test_pager_class_cannot_hide_additional_publication_content(self):
        self.soup.select_one('.pb_sys_common').append(' 教学安排')
        self.assertIsNone(self.recover())

    def test_pager_controls_cannot_hide_an_article_link(self):
        link = self.soup.new_tag('a', href='../info/1024/99999.htm')
        link.string = '关于本学期招生工作的通知'
        self.soup.select_one('.pb_sys_common').append(link)
        self.assertIsNone(self.recover())

    def test_clickable_or_nested_label_is_not_a_plain_heading(self):
        title = self.soup.select_one('.list-right-title > p')
        title.clear()
        link = self.soup.new_tag('a', href='other.htm')
        link.string = '招生信息'
        title.append(link)
        self.assertIsNone(self.recover())
