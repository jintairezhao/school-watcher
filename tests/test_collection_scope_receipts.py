"""Collection consumes the same current executed scope as the other readers."""
from datetime import datetime,timedelta
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement
from backend.services import source_governance as governance
from tests.test_source_path_receipts import SourcePathReceiptTests


class CollectionScopeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture=SourcePathReceiptTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source=self.fixture.source
        self.first=self.document(1)
        self.second=self.document(2)
        self.detail=self.fixture.detail(region={'container':'#notices','heading':'#notices .caption','name':'通知公告'},
            list_snapshot={'hash':governance._hash(self.first),'url':self.source.list_url,'outcome':'usable'})
        self.fixture.detail=lambda **changes:{**self.detail,**changes}

    def document(self,page):
        items=''.join(f'<li><a href="/info/{page}{i}.htm">关于本学期学生教学事项安排的通知{page}{i}</a><span>2026-09-01</span></li>' for i in range(3))
        pager='<a rel="next" href="?page=2">下一页</a>' if page==1 else ''
        return f'<div id="notices"><div class="caption">通知公告</div><ul>{items}</ul>{pager}</div>'

    def collect(self):
        from backend.scraper.engine import scrape_department
        reads=[]
        def fetch(url,**kwargs):
            reads.append(url)
            self.assertIn(url,(self.source.list_url,self.source.list_url+'?page=2'))
            return self.first if url==self.source.list_url else self.second
        with patch('backend.scraper.engine._fetch_html',side_effect=fetch), \
             patch('backend.scraper.engine.save_element_signatures'), \
             patch('backend.scraper.selector_monitor.evaluate_and_repair',return_value={'action':'healthy'}), \
             patch('backend.scraper.selector_monitor.queue_parser_repair',return_value=None), \
             patch('backend.scraper.discovery.publication_lists.publication_lists',return_value=[]), \
             patch('socket.socket.connect',side_effect=AssertionError('No website or model request')):
            result=scrape_department(self.source,self.fixture.school.url)
        return result,reads

    def test_activated_public_scope_collects_both_pages_without_a_native_validation(self):
        self.fixture.public()
        result,reads=self.collect()
        self.assertEqual((result,len(reads)),((6,6),2))
        self.assertEqual(Announcement.query.count(),6)

    def test_newer_failed_native_does_not_revive_an_older_successful_scope(self):
        from backend.scraper.selector_monitor import ParserRepairPending
        self.fixture.native(at=datetime.utcnow()-timedelta(minutes=2))
        self.fixture.native(self.fixture.detail(status='rejected',articles={'passed':False}))
        with self.assertRaises(ParserRepairPending):self.collect()
        self.assertEqual(Announcement.query.count(),3,'Only the first page was read before scope failed')

    def test_a_draft_proposal_does_not_supply_collection_scope(self):
        from backend.scraper.selector_monitor import ParserRepairPending
        self.fixture.public(state='pending')
        with self.assertRaises(ParserRepairPending):self.collect()
        self.assertEqual(Announcement.query.count(),3)


if __name__=='__main__':unittest.main()
