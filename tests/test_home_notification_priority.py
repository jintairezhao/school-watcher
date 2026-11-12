"""The actual faculty homepage main notice entry precedes a student news route."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import gzip
import hashlib
import json
import unittest

from backend.scraper.discovery.structure import extract_structure, publication_evidence, add_publication_structure
from backend.scraper.discovery.layered import route_structure, navigation_candidates, ordered_navigation
from backend.scraper.discovery.notification_entrances import notification_blocks, saved_notification_hint
from backend.scraper.discovery.frontier import choose_route
from backend.services.discovery_policy import get_policy
from backend.services.source_inventory import Inventory

ROOT='https://www.cupk.edu.cn/'
HOME=ROOT+'gxy/'
NOTICE=HOME+'tzgg/'
NEWS=HOME+'xsgz/xwkd/'
DIGEST='22f2097c1c024ba3be6255421700ddcc9dca6211a70dd8b65d9fdea5634123de'


class HomeNotificationPriorityTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).parent/'fixtures/structure/cupk-faculty-main-notice-22f2097c-20261023.html.gz'
        self.html=gzip.decompress(path.read_bytes()).decode('utf8')
        self.assertEqual(hashlib.sha256(self.html.encode()).hexdigest(),DIGEST)
        self.page=dict(url=HOME,kind='unit',label='化工与环境学院',discovery_policy='valuable',
            state='fetched',status_code=200,content_hash=DIGEST,path_json='[]')
        self.parsed=extract_structure(self.html,HOME,ROOT,'unit',self.page['label'])
        self.feed=publication_evidence(self.html,HOME)
        add_publication_structure(self.parsed,self.feed)
        for column in self.feed.get('lists',[self.feed]):
            if column.get('name')and column.get('column_url'):
                self.parsed['links'].append(dict(url=column['column_url'],label=column['name'],kind='channel',
                    path=[self.page['label']],locator=column['column_link_locator'],decision='follow',publication_route=True))
        route_structure(self.parsed,self.page,self.html,ROOT,True,policy='valuable',publication=self.feed)
        self.candidates=navigation_candidates(self.html,self.parsed,self.page)
        self.parent=dict(self.page,feed_json=json.dumps(self.feed))
        self.hint=next(h for h in self.parsed['primary_notification_routes']if h['column_url']==NOTICE)

    def test_actual_main_block_precedes_student_news_without_erasing_the_student_path(self):
        before=deepcopy(self.candidates)
        ordered=ordered_navigation(self.candidates,self.page,self.parsed,{},get_policy(),0)
        urls=[link['url']for link,_ in ordered]
        self.assertLess(urls.index(NOTICE),urls.index(NEWS))
        self.assertEqual(before,self.candidates)
        news=next(link for link,_ in ordered if link['url']==NEWS)
        self.assertIn('学生工作',news['path'])
        self.assertEqual({id(pair)for pair in ordered},{id(pair)for pair in self.candidates})

    def test_saved_paid_order_is_preserved_even_when_it_puts_news_before_the_notice(self):
        saved=sorted(self.candidates,key=lambda pair:pair[0]['url']!=NEWS)
        checkpoint={'route_candidate_order':[[link['url'],link['label']]for link,_ in saved]}
        ordered=ordered_navigation(self.candidates,self.page,self.parsed,checkpoint,get_policy(),0)
        self.assertEqual([link['url']for link,_ in ordered],[link['url']for link,_ in saved])

    def test_menu_words_nested_group_and_incomplete_material_do_not_make_a_main_block(self):
        self.assertFalse(notification_blocks('unit',HOME,DIGEST,None))
        column=next(c for c in self.feed['lists']if c.get('column_url')==NOTICE)
        for change in ({'column_group_name':'学生工作'},{'column_link_locator':''},{'heading_locator':''}):
            self.assertFalse(notification_blocks('unit',HOME,DIGEST,dict(column,**change)))
        self.assertFalse(notification_blocks('channel',HOME,DIGEST,column))
        self.assertIsNone(saved_notification_hint(dict(self.parent,state='pending'),NOTICE,self.hint['entry_locator']))

    def frontier(self,parent=None,edge_change=None,notice_change=None):
        parent=self.parent if parent is None else parent
        notice=dict(url=NOTICE,label='通知公告',kind='channel',depth=2,priority=3,state='pending',path_json='[]',frontier_rowid=2)
        news=dict(url=NEWS,label='新闻快递',kind='channel',depth=2,priority=3,state='pending',path_json='["学生工作"]',frontier_rowid=1)
        notice.update(notice_change or{})
        edge=dict(parent_url=HOME,target_url=NOTICE,label='通知公告',kind='channel',decision='follow',content_hash=DIGEST,locator=self.hint['entry_locator'])
        edge.update(edge_change or{})
        edges=[edge,dict(parent_url=HOME,target_url=NEWS,label='新闻快递',kind='channel',decision='follow',content_hash=DIGEST,locator='#student-news')]
        trace=[]
        result=choose_route([news,notice],edges=edges,parents={HOME:parent},attention={},
            history={HOME:parent,NOTICE:notice,NEWS:news},claim_count=0,explain=trace.append)
        return result,trace

    def test_production_frontier_gives_unread_main_notice_its_finite_turn(self):
        result,trace=self.frontier()
        self.assertEqual(result['url'],NOTICE)
        self.assertEqual(trace[0]['reason'],'unread_unit_main_notifications')
        self.assertEqual(trace[0]['attention']['trunk_evidence'][0]['entry_locator'],self.hint['entry_locator'])

    def test_stale_edge_wrong_locator_and_already_observed_notice_do_not_keep_the_boost(self):
        for kwargs in ({'edge_change':{'content_hash':'a'*64}},
                       {'edge_change':{'locator':'#unrelated-menu'}},
                       {'notice_change':{'content_hash':'already-observed'}}):
            with self.subTest(kwargs=kwargs):
                result,trace=self.frontier(**kwargs)
                self.assertIn(result['url'],(NOTICE,NEWS))
                self.assertNotEqual(trace[0]['reason'],'unread_unit_main_notifications')
                self.assertFalse(any(item.get('basis')=='observed_unit_home_notification'
                    for item in trace[0]['attention'].get('trunk_evidence',[])))

    def test_real_inventory_parser_and_claim_keep_the_notice_a_route_not_an_installed_source(self):
        from backend.scraper.discovery.inventory_crawler import _inspect_page
        with TemporaryDirectory()as temp:
            inventory=Inventory(Path(temp)/'inventory.sqlite3')
            key=inventory.ensure_site('中国石油大学（北京）克拉玛依校区',ROOT)
            inventory.finish(key,ROOT,html='<title>校区</title>',state='fetched',status_code=200)
            inventory.enqueue(key,HOME,self.page['label'],'unit',1,[],'school_domain')
            def retained(url):
                self.assertEqual(url,HOME)
                return dict(url=HOME,status=200,html=self.html)
            _inspect_page(inventory,inventory.report(key)['site'],inventory.get_page(key,HOME),
                          ai_assist=False,focus='valuable',fetcher=retained)
            chosen=inventory.claim(key,focus='valuable')
            self.assertEqual(chosen['url'],NOTICE)
            self.assertEqual(chosen['kind'],'channel')
            self.assertIn('学生工作',json.loads(inventory.get_page(key,NEWS)['path_json']))
            self.assertEqual(inventory.exploration_choices(key)[-1]['reason'],'unread_unit_main_notifications')


if __name__=='__main__':unittest.main()
