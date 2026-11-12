"""Pagination belongs to the actual selected list, including sibling pagers."""
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, Department
from tests import test_direct_onboarding as fixture_module

URL = fixture_module.URL
POLICY_ITEMS = '<li><a href="/info/1.htm">学生办事公开制度事项通知</a></li><li><a href="/info/2.htm">奖学金评选公开规定事项通知</a></li>'
MAIN_ITEMS = '<li><a href="/other/1.htm">另一个栏目的注册安排通知</a></li><li><a href="/other/2.htm">另一个栏目的竞赛安排通知</a></li>'
MIXED = ('<html><body><aside><section id="policies"><h2>信息公开制度</h2><ul>' + POLICY_ITEMS +
         '</ul></section></aside><main><section id="latest"><h2>最新通知</h2><div id="wp_news_w6"><ul>' +
         MAIN_ITEMS + '</ul></div><div id="wp_paging_w6"><a href="?latest=2" rel="next">下一页</a></div></section></main></body></html>')


class PaginationScopeTests(unittest.TestCase):
    def next(self, html, selector, scope=None):
        from backend.scraper.engine import _next_page_url
        return _next_page_url(html, URL, 1, list_selector=selector, scope=scope)

    def test_list_scope_is_required_instead_of_whole_page_default(self):
        from backend.scraper.engine import _next_page_url
        with self.assertRaises(TypeError):
            _next_page_url(MIXED, URL, 1)

    def test_empty_list_scope_is_explicitly_rejected(self):
        from backend.scraper.engine import PaginationScopeError
        for selector in ('', None, '   '):
            with self.subTest(selector=selector), self.assertRaises(PaginationScopeError):
                self.next(MIXED, selector)

    def test_sibling_pager_belongs_to_main_list_not_sidebar(self):
        self.assertIsNone(self.next(MIXED, '#policies li'))
        self.assertEqual(self.next(MIXED, '#wp_news_w6 li'), URL + '?latest=2')

    def test_two_adjacent_regions_keep_their_distinct_next_pages(self):
        html = MIXED.replace('</ul></section></aside>', '</ul><a rel="next" href="?policy=2">下一页</a></section></aside>')
        self.assertEqual(self.next(html, '#policies li'), URL + '?policy=2')
        self.assertEqual(self.next(html, '#wp_news_w6 li'), URL + '?latest=2')

    def test_unwrapped_peer_headings_keep_dom_order_ownership(self):
        html = '<section><h2>甲栏目</h2><ul id="a">' + POLICY_ITEMS + '</ul><a rel="next" href="?a=2">下一页</a><h2>乙栏目</h2><ul id="b">' + MAIN_ITEMS + '</ul><a rel="next" href="?b=2">下一页</a></section>'
        self.assertEqual(self.next(html, '#a li'), URL + '?a=2')
        self.assertEqual(self.next(html, '#b li'), URL + '?b=2')

    def test_top_pager_after_own_heading_is_not_previous_columns_pager(self):
        html = '<section><h2>甲栏目</h2><ul id="a">' + POLICY_ITEMS + '</ul><h2>乙栏目</h2><a rel="next" href="?b=2">下一页</a><ul id="b">' + MAIN_ITEMS + '</ul></section>'
        self.assertIsNone(self.next(html, '#a li'))
        self.assertEqual(self.next(html, '#b li'), URL + '?b=2')

    def test_article_number_and_cross_host_link_are_not_pagination(self):
        html = '<section><ul id="a"><li><a href="/article-2.htm">2</a></li></ul><a rel="next" href="https://unrelated.edu.cn/page2">下一页</a></section>'
        self.assertIsNone(self.next(html, '#a li'))

    def test_ambiguous_multiple_selected_lists_never_fall_back_to_whole_page(self):
        from backend.scraper.engine import PaginationScopeError
        with self.assertRaises(PaginationScopeError):
            self.next(MIXED, 'li')

    def test_selector_without_matches_is_not_proof_of_no_pagination(self):
        from backend.scraper.engine import PaginationScopeError
        with self.assertRaises(PaginationScopeError):
            self.next(MIXED, '#missing li')

    def test_identical_card_wrappers_are_distinct_dom_regions(self):
        from backend.scraper.engine import PaginationScopeError
        card = '<div><h2>学生通知</h2><p class="entry"><a href="/info/1.htm">学生事项通知</a></p></div>'
        with self.assertRaises(PaginationScopeError):
            self.next('<section>' + card + card + '<a rel="next" href="?p=2">下一页</a></section>', '.entry')

    def test_div_card_neighbor_and_div_heading_do_not_supply_ul_pagination(self):
        html = ('<section><div class="title">甲栏目</div><ul id="a">' + POLICY_ITEMS +
                '</ul><div class="title">乙栏目</div><div id="cards"><article><a href="/other/1">注册安排通知</a></article>'
                '<article><a href="/other/2">竞赛安排通知</a></article></div><a rel="next" href="?cards=2">下一页</a></section>')
        self.assertIsNone(self.next(html, '#a li', {'container_selector': 'section', 'heading_selector': '.title:first-child'}))

    def test_tight_list_container_retains_its_actual_sibling_pager(self):
        scope = {'container_selector': '#wp_news_w6', 'heading_selector': '#latest h2'}
        self.assertEqual(self.next(MIXED, '#wp_news_w6 li', scope), URL + '?latest=2')

    def test_unknown_single_card_and_different_heading_cannot_supply_previous_lists_pager(self):
        html = ('<section><h2>制度栏目</h2><ul id="a"><li><a href="/a/1.htm">制度公告事项</a></li></ul>'
                '<div id="neighbor"><div class="other-title">最新通知</div>'
                '<div class="entry"><span><a href="/b/1.htm">邻栏公告事项</a></span></div>'
                '<a rel="next" href="?b=2">下一页</a></div></section>')
        self.assertIsNone(self.next(html, '#a li'))

    def counted_pager(self):
        # Literal first/last labels and counter grammar from the retained
        # safety/psychology pages; no CSS marker supplies pager ownership.
        return ('<div><table><tr><td><a href="#">第一页</a><a href="#">上一页</a>'
                '<a href="index_2.shtml"><span>下一页</span></a><a href="index_3.shtml">最末页</a>'
                '<span>共15条记录，每页6条</span></td></tr></table></div>')

    def test_counted_first_last_controls_do_not_become_foreign_article_links(self):
        html = '<section id="column"><h2>通知公告</h2><ul id="a">'+POLICY_ITEMS+'</ul>'+self.counted_pager()+'</section>'
        scope = dict(container_selector='#column',heading_selector='#column h2')
        self.assertEqual(self.next(html,'#a li',scope), URL+'index_2.shtml')

    def test_counted_pager_can_be_a_sibling_of_the_complete_column_container(self):
        html = '<main><section id="column"><h2>通知公告</h2><ul id="a">'+POLICY_ITEMS+'</ul></section>'+self.counted_pager()+'</main>'
        scope = dict(container_selector='#column',heading_selector='#column h2')
        self.assertEqual(self.next(html,'#a li',scope), URL+'index_2.shtml')

    def test_single_card_named_first_page_is_still_a_foreign_article(self):
        html = ('<main><ul id="a">'+POLICY_ITEMS+'</ul><section><div><a href="/story.htm">第一页</a></div>'
                '<a href="?neighbor=2">下一页</a></section></main>')
        self.assertIsNone(self.next(html,'#a li'))

    def test_sibling_pager_exception_requires_local_controls_and_keeps_other_boundaries(self):
        scope = dict(container_selector='#column',heading_selector='#column h2')
        column = '<section id="column"><h2>通知公告</h2><ul id="a">'+POLICY_ITEMS+'</ul></section>'
        for tail in ('<footer>'+self.counted_pager()+'</footer>',
                     '<nav>'+self.counted_pager()+'</nav>',
                     '<section><h2>其他栏目</h2>'+self.counted_pager()+'</section>',
                     '<section><a href="/neighbor.htm">邻栏真实通知</a>'+self.counted_pager()+'</section>',
                     '<a href="?unknown=2">下一页</a>'):
            with self.subTest(tail=tail):
                self.assertIsNone(self.next('<main>'+column+tail+'</main>','#a li',scope))
        self.assertIsNone(self.next('<main><div>'+column+'</div>'+self.counted_pager()+'</main>','#a li',scope))

    def test_counted_controls_cannot_hide_an_unrecognized_article_in_their_wrapper(self):
        html = ('<main><ul id="a">'+POLICY_ITEMS+'</ul><section><div><a href="/neighbor.htm">邻栏真实通知</a></div>'
                +self.counted_pager()+'</section></main>')
        self.assertIsNone(self.next(html,'#a li'))

    def single_record_wrappers(self):
        # Reduced from retained xxglb markup: each real row has its own ul,
        # while one content wrapper and a sibling pager serve the whole list.
        return ('<div id="items">' + ''.join(
            '<ul class="entry"><li><a href="/notice/' + str(i) + '.htm">正式通知事项' + str(i) + '</a></li></ul>'
            for i in range(3)) + '</div>')

    def test_single_record_wrappers_share_one_sibling_pager(self):
        html = '<main><h2>通知公告</h2>' + self.single_record_wrappers() + '<div class="pager"><a href="?p=2">下一页</a></div></main>'
        for selector in ('#items li', '#items > ul'):
            with self.subTest(selector=selector):
                self.assertEqual(self.next(html, selector), URL + '?p=2')

    def test_partial_selection_of_single_record_wrappers_is_not_a_complete_group(self):
        from backend.scraper.engine import PaginationScopeError
        html = '<main>' + self.single_record_wrappers() + '<a href="?p=2">下一页</a></main>'
        with self.assertRaises(PaginationScopeError):
            self.next(html, '#items ul:nth-child(-n+2) li')

    def test_single_record_wrappers_do_not_take_a_neighbor_pager(self):
        html = ('<main><h2>通知公告</h2>' + self.single_record_wrappers() +
                '<h2>其他栏目</h2><div id="neighbor"><ul>' + MAIN_ITEMS +
                '</ul><a href="?neighbor=2">下一页</a></div></main>')
        self.assertIsNone(self.next(html, '#items li'))
        self.assertEqual(self.next(html, '#neighbor li'), URL + '?neighbor=2')

    def test_peer_headings_keep_single_record_lists_separate(self):
        from backend.scraper.engine import PaginationScopeError
        html = '<div><h2>甲栏目</h2><ul><li><a href="/a">甲通知事项</a></li></ul><h2>乙栏目</h2><ul><li><a href="/b">乙通知事项</a></li></ul><a href="?p=2">下一页</a></div>'
        with self.assertRaises(PaginationScopeError):
            self.next(html, 'li')

    def test_multiple_real_lists_with_several_rows_are_not_single_record_wrappers(self):
        from backend.scraper.engine import PaginationScopeError
        html = '<div><ul>' + POLICY_ITEMS + '</ul><ul>' + MAIN_ITEMS + '</ul><a href="?p=2">下一页</a></div>'
        with self.assertRaises(PaginationScopeError):
            self.next(html, 'li')


class SourcePaginationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def sidebar_column(self):
        return fixture_module.ai_column(name='信息公开制度', list_selector='#policies li',
            container_selector='#policies', heading_selector='#policies h2', date_selector='', content_selector='article')

    def body(self, url):
        title = ('学生办事公开制度事项通知' if url.endswith('1.htm') else '奖学金评选公开规定事项通知')
        return '<title>' + title + '</title><article><p>为帮助在校学生办理公开事项并查询相关规定，学校公布以下具体安排，请按通知说明准备材料并在规定时间办理。</p></article>'

    def source(self):
        source = Department(school_id=self.fixture.school_id, **{key: value for key, value in
            dict(self.sidebar_column(), list_url=URL).items() if key not in ('container_selector', 'heading_selector')})
        db.session.add(source); db.session.commit()
        return source

    def validated_scope(self, source):
        from backend.services.source_governance import source_config, _hash
        from backend.database.source_governance_models import SourceValidation
        config_hash = _hash(source_config(source))
        for detail in ({'status': 'full', 'region': {'name': source.name, 'container': '#policies', 'heading': '#policies h2'}},
                       {'status': 'rejected', 'region': {'name': source.name, 'container': '#latest', 'heading': '#latest h2'}}):
            db.session.add(SourceValidation(school_id=source.school_id, department_id=source.id,
                config_hash=config_hash, validation_json=json.dumps(detail)))
        db.session.commit()

    def daily(self, source, fetch):
        from backend.scraper.engine import scrape_department
        with patch('backend.scraper.engine._fetch_html', side_effect=fetch) as read, \
             patch('backend.scraper.engine.save_element_signatures'), \
             patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action': 'healthy'}):
            result = scrape_department(source, URL)
        return result, read.call_count

    def test_onboarding_unpaginated_sidebar_does_not_read_neighbor_next(self):
        from backend.services.source_onboarding import onboard_page
        reads = []
        def fetch(url, purpose):
            reads.append((url, purpose))
            if url == URL:
                return MIXED
            self.assertNotIn('latest=2', url, 'Neighbor pagination must not be read')
            return self.body(url)
        result = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False,
                               'observed_columns': [self.sidebar_column()]}, fetcher=fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 2)
        self.assertTrue(all('latest=2' not in address for address, _ in reads))

    def test_daily_collection_never_fetches_neighbor_pagination(self):
        source = self.source()
        def fetch(url, **kwargs):
            self.assertEqual(url, URL)
            return MIXED
        result, reads = self.daily(source, fetch)
        self.assertEqual(reads, 1)
        self.assertEqual(result, (2, 2))

    def test_true_same_column_page_two_is_collected(self):
        source = self.source()
        self.validated_scope(source)
        first = MIXED.replace('</ul></section></aside>', '</ul><a rel="next" href="?policy=2">下一页</a></section></aside>')
        second = MIXED.replace(POLICY_ITEMS, '<li><a href="/info/3.htm">第三项真实学生事项公开通知</a></li>')
        reads = []
        def fetch(url, **kwargs):
            reads.append(url)
            return first if url == URL else second
        result, count = self.daily(source, fetch)
        self.assertEqual(reads, [URL, URL + '?policy=2'])
        self.assertEqual((result, count), ((3, 3), 2))
        self.assertEqual(Announcement.query.count(), 3)

    def test_later_page_heading_contradiction_is_rejected_before_ingestion(self):
        from backend.scraper.selector_monitor import ParserRepairPending
        source = self.source()
        self.validated_scope(source)
        first = MIXED.replace('</ul></section></aside>', '</ul><a rel="next" href="?policy=2">下一页</a></section></aside>')
        second = MIXED.replace('信息公开制度', '隔壁招生信息').replace(POLICY_ITEMS, '<li><a href="/other/3.htm">不属于当前栏目的招生事项通知</a></li>')
        with patch('backend.scraper.selector_monitor.queue_parser_repair', return_value=None) as repair:
            with self.assertRaises(ParserRepairPending):
                self.daily(source, lambda url, **kwargs: first if url == URL else second)
        repair.assert_called_once()
        self.assertEqual({row.url for row in Announcement.query.all()}, {URL.rsplit('/notices/', 1)[0] + '/info/1.htm', URL.rsplit('/notices/', 1)[0] + '/info/2.htm'})

    def test_legacy_neighbor_cursor_and_old_selectors_are_not_reused(self):
        source = self.source()
        saved = {'page': 2, 'page_url': URL + '?latest=2', 'new': 0, 'total': 2, 'finished': False,
                 'selectors': {'list_selector': '#wp_news_w6 li'}}
        progress = []
        def fetch(url, **kwargs):
            self.assertEqual(url, URL)
            return MIXED
        with patch('backend.services.source_collection.collection_progress', return_value=saved), \
             patch('backend.services.source_collection.save_collection_progress', side_effect=lambda key, data: progress.append(data)):
            result, count = self.daily(source, fetch)
        self.assertEqual(result, (2, 4))
        self.assertEqual(count, 1)
        self.assertEqual(progress[0]['page_url'], URL)
        self.assertEqual(progress[-1]['selectors']['list_selector'], '#policies li')
        self.assertEqual(Announcement.query.count(), 2)
        self.assertFalse(any('/other/' in row.url for row in Announcement.query.all()))

    def test_changed_rule_discards_even_a_previously_scoped_cursor(self):
        source = self.source()
        saved = {'page': 2, 'page_url': URL + '?latest=2', 'new': 0, 'total': 2, 'finished': False,
                 'pagination_scope_version': 1, 'scope_hash': 'previous-rule-and-region',
                 'selectors': {name: getattr(source, name) for name in
                     ('list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector')}}
        def fetch(url, **kwargs):
            self.assertEqual(url, URL)
            return MIXED
        with patch('backend.services.source_collection.collection_progress', return_value=saved), \
             patch('backend.services.source_collection.save_collection_progress'):
            result, count = self.daily(source, fetch)
        self.assertEqual((result, count), ((2, 4), 1))
        self.assertEqual(Announcement.query.count(), 2)

    def test_legacy_unscoped_pagination_validation_cannot_activate_after_upgrade(self):
        from backend.database.source_governance_models import SourceProposal
        from backend.services import source_governance as g
        source = self.source()
        candidate = g._json(g.source_config(source))
        for version in ('source-governance-7','source-governance-8'):
            with self.subTest(version=version):
                proposal = SourceProposal(school_id=source.school_id, department_id=source.id, identity_key='same-source',
                    proposal_key='old-pager-validation-'+version, origin='discovery', state='validated', candidate_json=candidate,
                    expected_config_hash=g._hash(g.source_config(source)), evidence_json='{}', evidence_hash=g._hash({}),
                    validator_version=version, validated_hash=g._hash([candidate, g._hash({}), version]))
                db.session.add(proposal); db.session.commit()
                with patch.object(g, 'read_snapshot', side_effect=AssertionError('Old validation must stop before reading or installing')):
                    with self.assertRaisesRegex(ValueError, '统一检查'):
                        g.activate_proposal(proposal.id)
                self.assertEqual(db.session.get(Department, source.id).list_selector, '#policies li')
                self.assertEqual(proposal.state, 'validated')

    def test_newly_observed_counted_pager_requires_its_own_validation_material(self):
        from backend.services import source_governance as g
        source = self.source()
        first = MIXED.replace('</ul></section></aside>', '</ul>'+PaginationScopeTests().counted_pager()+'</section></aside>')
        config = g.source_config(source)
        scope = dict(container_selector='#policies',heading_selector='#policies h2')
        def fetch(url,purpose):
            return first if purpose in ('list','independent_list') else self.body(url)
        captured = g.capture_source_evidence(self.fixture.school_id,config,department_id=source.id,
            seed_html=first,scope_evidence=scope,fetcher=fetch)
        self.assertIn('pagination',captured.bundle)
        captured.bundle.pop('pagination')
        proposal = g.propose_source(self.fixture.school_id,config,captured,department_id=source.id)
        result = g.validate_proposal(proposal.id)
        self.assertIn('pagination_sample_missing',result['errors'])
        self.assertFalse(result['passed'])


if __name__ == '__main__':
    unittest.main()
