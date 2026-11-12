"""A configured multi-list page is finite without claiming full column history."""
from contextlib import ExitStack
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.services.source_collection import collect_source
from tests import test_direct_onboarding as fixture_module

URL = 'https://example.edu.cn/'


def document(pager=''):
    def region(key, name, offset):
        items = ''.join(f'<li><a href="/info/{i}.htm">关于本学期学生校园事务安排的通知{i}</a>'
                        '<span>2026-10-01</span></li>' for i in range(offset, offset + 3))
        return f'<section id="{key}"><h2>{name}</h2><ul>{items}</ul>' + (pager if key == 'news' else '') + '</section>'
    return '<title>测试大学</title><main>' + region('notices', '通知公告', 1) + region('news', '学院新闻', 4) + '</main>'


class HomepageUnionCollectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.source = Department(school_id=self.fixture.school_id, name='主页组合范围', list_url=URL,
            list_selector='#notices li, #news li', title_selector='a', link_selector='a', date_selector='span',
            content_selector='article', group_name='测试大学')
        db.session.add(self.source)
        db.session.commit()
        self.checkpoints = []

    def collect(self, html=None, saved=None, receipts=None):
        html = document() if html is None else html
        with ExitStack() as guards:
            guards.enter_context(patch('socket.socket.connect', side_effect=AssertionError('No website or model request')))
            read = guards.enter_context(patch('backend.scraper.engine._fetch_html', return_value=html))
            repair = guards.enter_context(patch('backend.scraper.selector_monitor.queue_parser_repair', return_value=None))
            coverage = guards.enter_context(patch('backend.services.collection_settings.record_coverage'))
            guards.enter_context(patch('backend.scraper.engine.save_element_signatures'))
            guards.enter_context(patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action': 'healthy'}))
            guards.enter_context(patch('backend.services.source_path_receipts.load_source_receipts', return_value=receipts or []))
            guards.enter_context(patch('backend.services.student_information.queue_source_assessment'))
            guards.enter_context(patch('backend.services.student_information.queue_listing_assessment'))
            guards.enter_context(patch('backend.services.source_collection.collection_progress', return_value=deepcopy(saved or {})))
            guards.enter_context(patch('backend.services.source_collection.save_collection_progress',
                                      side_effect=lambda key, value: self.checkpoints.append(deepcopy(value))))
            result = collect_source(self.source)
        return result, read, repair, coverage

    def test_every_selected_region_is_collected_without_parser_repair(self):
        result, read, repair, coverage = self.collect()
        self.assertFalse(result.get('partial'), result)
        self.assertEqual((result['new_count'], result['checked']), (6, 6))
        self.assertEqual(Announcement.query.count(), 6)
        self.assertEqual(read.call_count, 1)
        repair.assert_not_called()
        self.assertEqual(result['collection_scope']['kind'], 'configured_multiple_regions')
        self.assertEqual(result['collection_scope']['coverage'], 'selected_page')
        self.assertFalse(result['collection_scope']['full_column_equivalent'])

    def test_finite_page_does_not_record_full_history_coverage(self):
        result, _, _, coverage = self.collect()
        self.assertFalse(result.get('partial'), result)
        coverage.assert_not_called()
        self.assertIsNotNone(self.source.last_scraped_at)
        self.assertTrue(self.checkpoints[-1]['finished'])
        self.assertEqual(self.checkpoints[-1]['collection_scope'], result['collection_scope'])

    def test_finished_checkpoint_restores_the_same_limited_scope(self):
        first, _, _, _ = self.collect()
        self.assertFalse(first.get('partial'), first)
        saved = self.checkpoints[-1]
        second, read, repair, coverage = self.collect(saved=saved)
        self.assertEqual(second['collection_scope'], first['collection_scope'])
        self.assertEqual(Announcement.query.count(), 6)
        read.assert_not_called()
        repair.assert_not_called()
        coverage.assert_not_called()

    def test_member_with_next_page_cannot_be_silently_marked_finished(self):
        result, read, repair, coverage = self.collect(document('<a rel="next" href="?news=2">下一页</a>'))
        self.assertTrue(result['partial'])
        self.assertNotIn('collection_scope', result)
        self.assertFalse(any(value['finished'] for value in self.checkpoints))
        self.assertEqual(Announcement.query.count(), 6)
        self.assertEqual(read.call_count, 1)
        repair.assert_called_once()
        coverage.assert_not_called()

    def test_script_only_next_control_does_not_prove_a_member_is_exhausted(self):
        result, _, repair, coverage = self.collect(document('<a href="javascript:nextPage()">下一页</a>'))
        self.assertTrue(result['partial'])
        self.assertNotIn('collection_scope', result)
        repair.assert_called_once()
        coverage.assert_not_called()

    def test_partial_region_selection_is_not_a_complete_homepage(self):
        self.source.list_selector = '#notices li:nth-child(-n+2), #news li'
        db.session.commit()
        result, _, repair, coverage = self.collect()
        self.assertTrue(result['partial'])
        self.assertNotIn('collection_scope', result)
        repair.assert_called_once()
        coverage.assert_not_called()

    def test_existing_single_column_receipt_cannot_be_broadened_by_union_detection(self):
        from backend.services.source_governance import source_config, _hash
        receipt = dict(source_id=self.source.id, config_hash=_hash(source_config(self.source)),
                       region=dict(container='#notices', heading='#notices h2'))
        result, _, repair, coverage = self.collect(receipts=[receipt])
        self.assertTrue(result['partial'])
        self.assertEqual(Announcement.query.count(), 0)
        repair.assert_called_once()
        coverage.assert_not_called()

    def test_ordinary_single_column_retains_full_history_accounting(self):
        self.source.name = '通知公告'
        self.source.list_selector = '#notices li'
        db.session.commit()
        result, _, repair, coverage = self.collect()
        self.assertFalse(result.get('partial'), result)
        self.assertNotIn('collection_scope', result)
        self.assertEqual((result['new_count'], result['checked']), (3, 3))
        repair.assert_not_called()
        coverage.assert_called_once()

    def test_one_homepage_preview_keeps_its_limited_identity(self):
        from tests.test_homepage_scope_modules import preview_html, preview_config
        config = preview_config()
        for key, value in config.items(): setattr(self.source, key, value)
        self.source.list_url = URL
        db.session.commit()
        result, _, repair, coverage = self.collect(preview_html())
        self.assertFalse(result.get('partial'), result)
        self.assertEqual(result['collection_scope']['kind'], 'configured_module_preview')
        self.assertEqual(result['checked'], 3)
        self.assertFalse(result['collection_scope']['full_column_equivalent'])
        repair.assert_not_called()
        coverage.assert_not_called()


if __name__ == '__main__':
    unittest.main()
