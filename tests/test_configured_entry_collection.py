"""A saved display name cannot replace a witnessed list entry's identity."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.services.source_governance import source_config
from tests.test_direct_onboarding import DirectOnboardingTests
from tests.test_zcms_notice_breadcrumb import document, URL as TEMPLATE_URL


URL = 'https://example.edu.cn/jwb/jxyx/tzgg/'
SCRIPT = '<script>function page(n){return "index_${PageIndex}.shtml".replace("${PageIndex}",n);}</script>'


def page(number=1, *, leaf=URL, name='通知公告', count=3):
    pager = '<div class="middleNotice__paging"><a href="index_2.shtml">下一页</a></div>' if number == 1 else ''
    html = document(extra=pager + SCRIPT, count=count).replace(TEMPLATE_URL, URL)
    if number > 1:
        html = html.replace('53858', '53958')
    return html.replace('href="' + URL + '">通知公告', 'href="' + leaf + '">' + name)


class ConfiguredEntryCollectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo'):
            guard = patch(name, side_effect=AssertionError('No website or model request'))
            guard.start()
            self.addCleanup(guard.stop)
        self.source = Department(school_id=self.fixture.school_id, name='教务部-教学运行',
            group_name='组织机构', list_url=URL, list_selector='li.middleNotice__notice--list',
            title_selector='a', link_selector='a', date_selector='.middleNotice--dates', content_selector='article')
        db.session.add(self.source)
        db.session.commit()

    def run_collection(self, second=None):
        from backend.scraper.engine import scrape_department
        reads = []
        def fetch(url, **kwargs):
            reads.append(url)
            self.assertIn(url, (URL, URL + 'index_2.shtml'))
            return page() if url == URL else second or page(2)
        with patch('backend.scraper.engine._fetch_html', side_effect=fetch), \
             patch('backend.scraper.engine.save_element_signatures'), \
             patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action':'healthy'}), \
             patch('backend.scraper.selector_monitor.queue_parser_repair', return_value=None):
            result = scrape_department(self.source, 'https://example.edu.cn/')
        return result, reads

    def test_witnessed_entry_collects_both_pages_without_renaming_saved_source(self):
        before = deepcopy(source_config(self.source))
        result, reads = self.run_collection()
        self.assertEqual(result, (6, 6))
        self.assertEqual(reads, [URL, URL + 'index_2.shtml'])
        self.assertEqual(Announcement.query.count(), 6)
        self.assertEqual(source_config(self.source), before)
        self.assertIsNotNone(self.source.last_scraped_at)

    def test_later_page_cannot_switch_to_another_named_column(self):
        from backend.scraper.selector_monitor import ParserRepairPending
        with self.assertRaises(ParserRepairPending):
            self.run_collection(page(2, name='学院新闻'))
        self.assertEqual(Announcement.query.count(), 3)
        self.assertIsNone(self.source.last_scraped_at)

    def test_entry_binding_requires_exact_self_link_complete_selection_and_one_region(self):
        from backend.scraper.engine import _configured_entry_region
        config = source_config(self.source)
        self.assertEqual(_configured_entry_region(config, page(), URL)['name'], '通知公告')
        self.assertIsNone(_configured_entry_region(config, page(leaf=URL + 'other/'), URL))
        self.assertIsNone(_configured_entry_region(dict(config, list_selector='li:nth-of-type(1)'), page(), URL))
        self.assertIsNone(_configured_entry_region(config, page().replace('c-main__cur', 'unrelated'), URL))
        self.assertIsNone(_configured_entry_region(config, page(2), URL + 'index_2.shtml'))

    def test_continuation_must_retain_the_witnessed_entry_name_and_pager_evidence(self):
        from backend.scraper.engine import _configured_entry_region
        config = source_config(self.source)
        scope = dict(entry_url=URL, name='通知公告')
        self.assertIsNotNone(_configured_entry_region(config, page(2), URL + 'index_2.shtml', binding=scope))
        self.assertIsNone(_configured_entry_region(config, page(2, name='学院新闻'), URL + 'index_2.shtml', binding=scope))
        self.assertIsNone(_configured_entry_region(config, page(2).replace(SCRIPT, ''), URL + 'index_2.shtml', binding=scope))

    def test_single_entry_final_page_is_collected_without_disabling_scope_checks(self):
        result, _ = self.run_collection(page(2,count=1))
        self.assertEqual(result, (4, 4))
        self.assertEqual(Announcement.query.count(), 4)

    def test_configured_pager_address_needs_its_observed_alias_evidence(self):
        from backend.scraper.engine import _configured_entry_region
        config=dict(source_config(self.source),list_url=URL+'index_1.shtml')
        binding=_configured_entry_region(config,page(),config['list_url'])
        self.assertEqual(binding['entry_url'],config['list_url'])
        self.assertEqual(binding['column_url'],URL)
        self.assertIsNone(_configured_entry_region(config,page().replace(SCRIPT,''),config['list_url']))
        self.assertIsNotNone(_configured_entry_region(config,page(2),URL+'index_2.shtml',binding=binding))

    def test_http_upgrade_requires_the_actual_requested_entry_not_protocol_similarity(self):
        from backend.scraper.engine import _configured_entry_region
        config=dict(source_config(self.source),list_url=URL.replace('https:','http:'))
        self.assertIsNone(_configured_entry_region(config,page(),URL))
        binding=_configured_entry_region(config,page(),URL,requested_url=config['list_url'])
        self.assertEqual(binding['configured_url'],config['list_url'])
        self.assertEqual(binding['entry_url'],URL)
        self.assertIsNotNone(_configured_entry_region(config,page(2),URL+'index_2.shtml',binding=binding))
        self.assertIsNone(_configured_entry_region(config,page(),URL,requested_url=URL+'other/'))

    def test_worker_handoff_preserves_the_bound_name_without_reading_first_page_again(self):
        from datetime import datetime, timedelta
        from backend.services import tasks
        job=tasks.enqueue('collect',self.source.id,{'school_id':self.source.school_id,'department_id':self.source.id})
        handle=tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), patch('backend.scraper.engine.MAX_PAGES',1):
            with self.assertRaises(tasks.TaskDeferred) as caught:
                self.run_collection()
        tasks.handoff(handle,caught.exception)
        db.session.refresh(job)
        progress=next(iter(job.checkpoint['collection_progress'].values()))
        self.assertEqual(progress['column_scope']['name'],'通知公告')
        job.available_at=datetime.utcnow()-timedelta(seconds=1)
        db.session.commit()
        resumed=tasks.claim(capabilities=['http'])
        with tasks.execution_scope(resumed):
            result,reads=self.run_collection()
        self.assertEqual(reads,[URL+'index_2.shtml'])
        self.assertEqual(result,(6,6))
        self.assertEqual(Announcement.query.count(),6)


if __name__ == '__main__':
    unittest.main()
