"""Complete module selections must survive single-item and lead-card layouts."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from backend.database.db import db
from backend.database.models import Subscription
from backend.database.source_governance_models import SourceConfigVersion
from backend.scraper.discovery.publication_lists import publication_lists
from backend.services import source_governance as g
from backend.services.source_scope import observe_joint_selection,hold_configured_joint_scope

HOME='https://college.example.edu.cn/'


def gqt_module(name,count,start=0):
    rows=''.join(f'<li class="c-gqt-module__half-li"><a title="关于校园事务办理的通知{i}" '
        f'href="/c/2026-01-01/{200000+i}.shtml">关于校园事务办理的通知{i}</a>'
        '<span class="c-gqt-module__detail-content-date">2026-01-01</span></li>'for i in range(start,start+count))
    return ('<div class="c-gqt-module__half-width"><div class="c-gqt-module__head">'
        f'<div class="c-gqt-module__head-title-container"><span class="c-gqt-module__head-title">{name}</span></div>'
        f'<div class="c-gqt-module__head-more"><a href="/{start}/list/">more</a></div></div>'
        f'<div class="c-gqt-module__detail"><ul>{rows}</ul></div></div>')


def preview_html():
    prefix='c-caiwu-module-new__'
    def row(i):
        return (f'<div class="{prefix}main-content"><div class="{prefix}main-content-words">'
            f'<a href="/c/2026-01-01/{300000+i}.shtml">关于校园事务安排的通知{i}</a></div>'
            f'<div class="{prefix}main-content-date"><a href="/c/2026-01-01/{300000+i}.shtml">2026-01-01</a></div></div>')
    return (f'<title>学院主页</title><div class="{prefix}content-box-middle">'
        f'<div class="{prefix}content-box-middle-top">'+row(0)+
        '<div><a href="/c/2026-01-01/300000.shtml">同一首条的简要介绍</a></div></div>'
        f'<div class="{prefix}content-box-middle-down">'+row(1)+row(2)+'</div>'
        f'<div class="{prefix}content-box-middle-more"><a href="/notices/">MORE&gt;&gt;</a></div></div>')


def preview_config():
    return dict(name='学院主页',group_name='示例学院',list_url=HOME,
        list_selector='.c-caiwu-module-new__content-box-middle .c-caiwu-module-new__main-content',
        title_selector='.c-caiwu-module-new__main-content-words a',
        link_selector='.c-caiwu-module-new__main-content-words a',
        date_selector='.c-caiwu-module-new__main-content-date a',content_selector='article')


class HomepageModuleTests(unittest.TestCase):
    def test_unnamed_preview_keeps_its_real_more_navigation_without_naming_the_module(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import _inspect_page
        html=preview_html()
        with TemporaryDirectory()as folder:
            inventory=Inventory(Path(folder)/'inventory.sqlite3')
            key=inventory.ensure_site('示例学校','https://example.edu.cn/')
            inventory.enqueue(key,HOME,'示例学院','unit',1,[],'school_domain')
            _inspect_page(inventory,inventory.report(key)['site'],inventory.get_page(key,HOME),
                ai_assist=False,fetcher=lambda url:dict(url=url,status=200,html=html))
            page=inventory.get_page(key,HOME)
            with inventory.connect()as connection:
                links=[dict(edge)for edge in connection.execute('SELECT * FROM edges WHERE site_key=? AND parent_url=? AND target_url=?',
                    (key,HOME,HOME+'notices/'))]
            self.assertEqual(len(links),1)
            self.assertEqual((links[0]['label'],links[0]['kind'],links[0]['decision']),('MORE>>','navigation','follow'))
            self.assertTrue(links[0]['locator'])
            feed=json.loads(page['feed_json'])
            preview=next(column for column in feed.get('lists',[feed])if column.get('module_preview'))
            self.assertEqual((preview['name'],preview['item_count']),('',3))
            self.assertFalse(preview['full_column_equivalent'])
            self.assertFalse(any(node['name']=='通知公告'for node in inventory.structure(key)))

    def test_single_named_download_item_completes_the_existing_union(self):
        html=gqt_module('活动通知',2)+gqt_module('下载专区',1,2)
        config=dict(preview_config(),list_selector='li.c-gqt-module__half-li',title_selector='a',
            link_selector='a',date_selector='.c-gqt-module__detail-content-date')
        feeds=publication_lists(html,HOME)
        self.assertEqual({f['name']:f['item_count']for f in feeds},{'活动通知':2,'下载专区':1})
        scope=observe_joint_selection(config,dict(url=HOME,hash=g._hash(html)),html)
        self.assertEqual((scope['kind'],scope['selected_items'],len(scope['regions'])),('configured_multiple_regions',3,2))
        self.assertFalse(scope['publisher_assertion'])

    def test_single_item_needs_its_own_visible_title_and_real_more_entrance(self):
        for change in ('title','duplicate','mixed_duplicate','more','foreign_widget','hidden','hidden_title','hidden_more'):
            with self.subTest(change=change):
                soup=BeautifulSoup(gqt_module('下载专区',1),'lxml')
                title=soup.select_one('.c-gqt-module__head-title')
                if change=='title':title['class']=[]
                elif change=='duplicate':title.parent.append(deepcopy(title))
                elif change=='mixed_duplicate':
                    other=deepcopy(title);other['class']=['c-gqt-module__head-title-color-green'];title.parent.append(other)
                elif change=='more':soup.select_one('a[href]').decompose()
                elif change=='hidden':soup.select_one('.c-gqt-module__half-width')['hidden']=''
                elif change=='hidden_title':title['hidden']=''
                elif change=='hidden_more':soup.select_one('.c-gqt-module__head-more')['style']='display: none'
                else:soup.select_one('.c-gqt-module__detail').append(BeautifulSoup('<section><a href="/other/">另一栏目</a></section>','lxml').section)
                self.assertEqual(publication_lists(str(soup),HOME),[])

    def test_green_title_variant_binds_only_its_own_module_and_more_link(self):
        html=gqt_module('主题教育',6).replace('class="c-gqt-module__head-title"',
            'class="c-gqt-module__head-title-color-green"')+gqt_module('下载专区',1,6)
        config=dict(preview_config(),list_selector='li.c-gqt-module__half-li',title_selector='a',
            link_selector='a',date_selector='.c-gqt-module__detail-content-date')
        scope=observe_joint_selection(config,dict(url=HOME,hash=g._hash(html)),html)
        self.assertEqual({r['name']:r['selected_items']for r in scope['regions']},{'主题教育':6,'下载专区':1})
        soup=BeautifulSoup(html,'lxml')
        for region in scope['regions']:
            title=soup.select_one(region['heading'])
            item=soup.select_one(region['list_selector'])
            more=soup.select_one(region['column_link'])
            module=item.find_parent(class_='c-gqt-module__half-width')
            self.assertIs(title.find_parent(class_='c-gqt-module__half-width'),module)
            self.assertIs(more.find_parent(class_='c-gqt-module__half-width'),module)
            self.assertEqual(title.get_text(strip=True),region['name'])

    def test_lead_and_lower_rows_are_one_unnamed_preview_not_a_four_row_subset(self):
        html=preview_html();config=preview_config()
        feeds=publication_lists(html,HOME)
        self.assertEqual(len(feeds),1)
        self.assertEqual((feeds[0]['item_count'],feeds[0]['name'],feeds[0]['column_url']),(3,'',HOME+'notices/'))
        self.assertTrue(feeds[0]['module_preview'])
        scope=observe_joint_selection(config,dict(url=HOME,hash=g._hash(html)),html)
        self.assertEqual((scope['kind'],scope['selected_items'],len(scope['regions'])),('configured_module_preview',3,1))
        self.assertTrue(scope['regions'][0]['complete_region'])
        self.assertEqual(scope['regions'][0]['name_status'],'unknown')
        self.assertFalse(scope['publisher_assertion'])
        narrowed=dict(config,list_selector='.c-caiwu-module-new__content-box-middle-down > .c-caiwu-module-new__main-content')
        self.assertIsNone(observe_joint_selection(narrowed,dict(url=HOME,hash=g._hash(html)),html))

    def test_foreign_summary_or_unrelated_module_cannot_supply_complete_preview(self):
        for change in ('summary','more','extra','hidden'):
            with self.subTest(change=change):
                soup=BeautifulSoup(preview_html(),'lxml')
                if change=='summary':soup.select_one('.c-caiwu-module-new__content-box-middle-top > div:not(.c-caiwu-module-new__main-content) a')['href']='/another-article/'
                elif change=='more':soup.select_one('.c-caiwu-module-new__content-box-middle-more a')['href']=HOME
                elif change=='hidden':soup.select_one('.c-caiwu-module-new__content-box-middle')['hidden']=''
                else:soup.select_one('.c-caiwu-module-new__content-box-middle').append(BeautifulSoup('<section><a href="/other/">另一个栏目</a></section>','lxml').section)
                self.assertFalse(any(f.get('module_preview')for f in publication_lists(str(soup),HOME)))

    def test_preview_kind_flows_to_graph_without_becoming_verified_ownership(self):
        from types import SimpleNamespace
        from backend.services.structure_facts import build_structure_facts
        from backend.services.structure_snapshot import graph_projection,source_choices
        config=preview_config();html=preview_html()
        source=dict(config,id=7,school_id=1,kind='column')
        scope=dict(observe_joint_selection(config,dict(url=HOME,hash=g._hash(html)),html),source_id=7,
            status='scope_incomplete',components=[dict(name='',preview_items=3,column_url=HOME+'notices/',
                status='entry_unconfirmed',full_column_equivalent=False)])
        site=dict(id=1,name='示例大学',root_url=HOME,site_key='college')
        pages=[dict(url=HOME,state='fetched',content_hash=g._hash(html),checked_at='2026-10-30T01:00:00+00:00')]
        facts=build_structure_facts(site=site,pages=pages,departments=[source],directory_entries=[],
            structure_nodes=[],navigation_edges=[],source_scopes=[scope])
        capture=dict(facts=facts,report=dict(site=site,pages=pages),department_records=[source],
            version=dict(epoch='preview-fixture',revision=1),attention_policy=dict(profiles=['teacher'],weights={}))
        graph=graph_projection(capture,SimpleNamespace(id=1,url=HOME,name='示例大学'),scope='exploration')
        node=next(n for n in graph['nodes']if n['id']=='source:7')
        self.assertEqual(node['scope_kind'],'configured_module_preview')
        self.assertEqual(node['kind_label'],'主页预览范围')
        self.assertEqual(node['source_scope']['selected_items'],3)
        self.assertEqual(node['verification'],'unknown')
        self.assertFalse(any(r['type']in('source_ownership','source_preview')for r in graph['relations']))
        choices=source_choices(capture)
        column=next(c for groups in choices.values()for group in groups for c in group['columns'])
        self.assertEqual(column['scope_kind'],'configured_module_preview')


class HomepagePreviewGuardTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_governance import SourceGovernanceTests
        self.fixture=SourceGovernanceTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.source=self.fixture.dept
        for field,value in preview_config().items():setattr(self.source,field,value)
        db.session.add(Subscription(user_id=self.fixture.user.id,school_id=self.source.school_id,department_ids=[self.source.id]))
        db.session.commit();self.original=g.source_config(self.source)

    def proposal(self):
        candidate=dict(self.original,name='通知公告',list_url=HOME+'notices/',list_selector='#notices li',title_selector='a',link_selector='a',date_selector='time')
        captured=g.capture_source_evidence(self.source.school_id,candidate,department_id=self.source.id,
            seed_html=self.fixture.html(),fetcher=self.fixture.fetcher(self.fixture.html()),inventory=self.fixture.identity)
        captured.bundle['exploration_pages']=[g._snapshot(preview_html(),HOME)]
        return g.propose_source(self.source.school_id,candidate,captured,department_id=self.source.id)

    def test_preview_cannot_be_replaced_by_a_complete_column_under_the_same_id(self):
        proposal=self.proposal();result=g.validate_proposal(proposal.id)
        self.assertIn('configured_joint_scope_requires_review',result['errors'])
        self.assertEqual(result['configured_scope']['kind'],'configured_module_preview')
        self.assertTrue(hold_configured_joint_scope(proposal))
        self.assertIn('主页预览',json.loads(proposal.validation_json)['workflow']['reason'])
        self.assertEqual(g.source_config(self.source),self.original)
        self.assertEqual(Subscription.query.one().department_ids,[self.source.id])
        self.assertEqual(SourceConfigVersion.query.count(),0)

    def test_previously_passed_draft_still_hits_the_activation_scope_guard(self):
        proposal=self.proposal()
        with patch('backend.services.source_scope.configured_joint_scope',return_value=None):
            self.assertTrue(g.validate_proposal(proposal.id)['passed'])
        with self.assertRaisesRegex(ValueError,'主页预览'):
            g.activate_proposal(proposal.id)
        self.assertEqual(g.source_config(self.source),self.original)
        self.assertEqual(SourceConfigVersion.query.count(),0)

    def test_preview_scope_label_renders_in_chinese_and_english(self):
        from flask import render_template_string
        for locale,label in [('zh-CN','主页预览范围'),('en','Homepage preview scope')]:
            with self.subTest(locale=locale),self.fixture.app.test_request_context('/',headers={'Cookie':'watcher_locale='+locale}):
                column=dict(department=self.source,label=self.source.name,url=self.source.list_url,lifecycle='active',
                    scope_kind='configured_module_preview',source_scope=dict(components=[]))
                html=render_template_string("{% from '_source_tree_choices.html' import column_choice with context %}{{column_choice(column)}}",
                    column=column,selected_ids=None,source_paths={},groups={})
                self.assertIn(label,BeautifulSoup(html,'lxml').get_text(' ',strip=True))
