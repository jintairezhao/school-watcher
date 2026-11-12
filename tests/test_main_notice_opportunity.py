"""A proved main notice entry gets its finite turn before more unknown units."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import gzip
import hashlib
import json
import unittest

from backend.scraper.discovery.attention import candidate_attention
from backend.scraper.discovery.frontier import choose_route
from backend.scraper.discovery.notification_entrances import notification_blocks
from backend.scraper.discovery.structure import publication_evidence
from backend.services.source_inventory import Inventory

ROOT = 'https://www.swpu.edu.cn/'
DATA = {
    'sgy': ('石油与天然气工程学院', 'cafd1fe9e1ba0e937b6cfcf4da1887c381dbe760e7aaed155d201c413097c1e3'),
    'dky': ('地球科学与技术学院', 'a8ca2dc80994de7fc754a5369aad13dabaa80fd7ec8f75800706e81b52120361')}


class MainNoticeOpportunityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.materials = {}
        for slug,(label,digest) in DATA.items():
            raw = gzip.decompress((Path(__file__).parent/'fixtures/structure'/
                f'swpu-{slug}-home-{digest[:8]}-20261101.html.gz').read_bytes())
            assert hashlib.sha256(raw).hexdigest()==digest
            html = raw.decode('utf-8')
            cls.materials[slug] = dict(html=html, feed=publication_evidence(html,f'http://www.swpu.edu.cn/{slug}/'),
                url=f'http://www.swpu.edu.cn/{slug}',final_url=f'http://www.swpu.edu.cn/{slug}/',
                kind='unit',label=label,state='fetched',status_code=200,content_hash=digest)

    def route(self, slug='sgy', *, claim_count=0, changed_edge=None, notice_observed=False):
        home = self.materials[slug]
        other_slug = 'dky' if slug=='sgy' else 'sgy'
        other = dict(url=self.materials[other_slug]['url'],label=DATA[other_slug][0],kind='unit',
                     depth=1,state='pending',path_json='[]',frontier_rowid=1)
        notice = dict(url=f'http://www.swpu.edu.cn/{slug}/tzgg.htm',label='通知公告',kind='channel',
                      depth=2,state='pending',path_json='[]',frontier_rowid=2)
        if notice_observed:notice['content_hash']='b'*64
        column = next(c for c in home['feed']['lists'] if c.get('column_url')==notice['url'])
        edge = dict(parent_url=home['url'],target_url=notice['url'],decision='follow',
                    locator=column['column_link_locator'],content_hash=home['content_hash'])
        edge.update(changed_edge or {})
        root = dict(url=ROOT,kind='root',state='fetched',status_code=200,content_hash='a'*64)
        parent = {k:v for k,v in home.items() if k not in ('html','feed')}
        parent['feed_json']=json.dumps(home['feed'],ensure_ascii=False)
        trace=[]
        chosen=choose_route([other,notice],edges=[edge,dict(parent_url=ROOT,target_url=other['url'],
            decision='follow',locator='#college-entry',content_hash='a'*64)],
            parents={ROOT:root,home['url']:parent},history={ROOT:root,home['url']:parent,other['url']:other,notice['url']:notice},
            attention={},claim_count=claim_count,explain=trace.append)
        return chosen,trace[0],notice,other

    def test_proved_main_notice_precedes_another_unread_college(self):
        chosen,trace,notice,other=self.route()
        self.assertLess(candidate_attention(notice)['score'],candidate_attention(other)['score'])
        self.assertEqual(chosen['url'],notice['url'])
        self.assertEqual(trace['reason'],'unread_unit_main_notifications')

    def test_tabbed_notice_group_proves_one_entry_without_merging_preview_lists(self):
        home=self.materials['dky'];feed=deepcopy(home['feed']);before=deepcopy(feed)
        hints=notification_blocks('unit',home['final_url'],home['content_hash'],feed)
        self.assertEqual(len(hints),1)
        self.assertEqual(hints[0]['column_url'],'http://www.swpu.edu.cn/dky/tzgg.htm')
        self.assertEqual(hints[0]['heading_basis'],'observed_notification_group_heading')
        self.assertGreater(len(hints[0]['preview_container_locators']),1)
        self.assertEqual(feed,before)
        self.assertTrue(any(c.get('heading_ambiguous') and not c.get('name') for c in feed['lists']))
        chosen,trace,notice,_=self.route('dky')
        self.assertEqual(chosen['url'],notice['url'])
        self.assertEqual(trace['reason'],'unread_unit_main_notifications')

    def test_group_name_without_the_same_explicit_heading_does_not_make_an_entry(self):
        home=self.materials['dky']
        column=next(c for c in home['feed']['lists'] if c.get('column_group_name')=='通知公告')
        for change in ({'column_group_locator':'#other'}, {'heading_group_reference':False},
                       {'name':'本科'}, {'column_group_name':'学生工作'}, {'column_link_locator':''}):
            with self.subTest(change=change):
                self.assertEqual(notification_blocks('unit',home['final_url'],home['content_hash'],dict(column,**change)),[])

    def test_stale_or_unlocated_main_notice_cannot_override_a_real_college_entry(self):
        for change in ({'content_hash':'c'*64},{'locator':'#unrelated'}):
            with self.subTest(change=change):
                chosen,trace,_,other=self.route(changed_edge=change)
                self.assertEqual(chosen['url'],other['url'])
                self.assertNotEqual(trace['reason'],'unread_unit_main_notifications')

    def test_observed_notice_ends_the_initial_preference(self):
        chosen,trace,_,other=self.route(notice_observed=True)
        self.assertEqual(chosen['url'],other['url'])
        self.assertNotEqual(trace['reason'],'unread_unit_main_notifications')

    def test_oldest_pending_fairness_turn_is_still_preserved(self):
        chosen,trace,_,other=self.route(claim_count=4)
        self.assertEqual(chosen['url'],other['url'])
        self.assertEqual(trace['reason'],'oldest_pending_turn')

    def test_real_homepage_parser_and_inventory_claim_select_the_entry(self):
        from backend.scraper.discovery.inventory_crawler import _inspect_page
        for slug,home in self.materials.items():
            with self.subTest(slug=slug),TemporaryDirectory() as temp:
                store=Inventory(Path(temp)/'inventory.sqlite3');key=store.ensure_site('西南石油大学',ROOT)
                store.finish(key,ROOT,html='<title>调度程序测试根节点</title>',state='fetched',status_code=200)
                other_slug='dky' if slug=='sgy' else 'sgy';other=self.materials[other_slug]
                store.enqueue(key,other['url'],other['label'],'unit',1,[],'school_domain')
                store.record_edges(key,ROOT,[dict(url=other['url'],label=other['label'],kind='unit',path=[],
                    locator='#college-entry',decision='follow')],store.get_page(key,ROOT)['content_hash'])
                store.enqueue(key,home['url'],home['label'],'unit',1,[],'school_domain')
                _inspect_page(store,store.report(key)['site'],store.get_page(key,home['url']),focus='valuable',
                    ai_assist=False,fetcher=lambda url:dict(url=home['final_url'],status=200,html=home['html']))
                chosen=store.claim(key,focus='valuable')
                self.assertEqual(chosen['url'],f'http://www.swpu.edu.cn/{slug}/tzgg.htm')
                self.assertEqual(store.exploration_choices(key)[-1]['reason'],'unread_unit_main_notifications')
