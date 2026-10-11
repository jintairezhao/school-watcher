"""Automatic source activation uses readable articles and preserves column identity."""
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.database.source_governance_models import SourceValidation
from tests import test_direct_onboarding as fixture_module

HTML, TITLES, URL, ai_column = fixture_module.HTML, fixture_module.TITLES, fixture_module.URL, fixture_module.ai_column


class SourceValidationTests(unittest.TestCase):
    def test_access_restricted_samples_are_not_rule_mismatches(self):
        from backend.scraper.acquisition import FetchResult, FetchFailure
        from backend.services.source_validation import article_samples
        def fetch(address, purpose):
            raise FetchFailure(FetchResult(address, status=403, outcome='denied',
                error_code='http_403', retry_after=30000))
        result = article_samples({'list_url': URL}, [
            {'url': URL + str(i) + '.htm', 'title': title} for i, title in enumerate(TITLES)], fetch)
        self.assertFalse(result['passed'])
        self.assertEqual(result['failure_kind'], 'access_restricted')
        self.assertEqual(result['retry_after'], 30000)
        self.assertEqual(result['sample_count'], 3)

    def test_two_valid_bodies_do_not_override_an_observed_wrong_article_identity(self):
        from backend.services.source_validation import article_samples
        def fetch(address, purpose):
            if address.endswith('/0.htm'):
                return '<article><h1>另一栏目的真实通知</h1><p>另一栏目通知的完整正文材料，请按该栏目的安排办理相关事项。</p></article>'
            return self.fixture.fetch(address, purpose)
        result = article_samples(dict(ai_column(), list_url=URL), [
            {'url': 'https://example.edu.cn/info/' + str(i) + '.htm', 'title': title}
            for i, title in enumerate(TITLES)], fetch)
        self.assertEqual(result['matched_count'], 2)
        self.assertFalse(result['passed'])
        self.assertEqual(result['failure_kind'], 'rule_error')
        self.assertEqual(result['samples'][0]['reason'], 'article_identity_conflict')

    def test_denied_pinned_sample_still_allows_two_verified_later_bodies(self):
        from backend.scraper.acquisition import FetchResult, FetchFailure
        from backend.services.source_validation import article_samples
        def fetch(address, purpose):
            if address.endswith('/0.htm'):
                raise FetchFailure(FetchResult(address, status=403, outcome='denied', error_code='http_403'))
            return self.fixture.fetch(address, purpose)
        result = article_samples(dict(ai_column(), list_url=URL), [
            {'url': 'https://example.edu.cn/info/' + str(i) + '.htm', 'title': title}
            for i, title in enumerate(TITLES)], fetch)
        self.assertTrue(result['passed'])
        self.assertEqual(result['matched_count'], 2)
        self.assertEqual(result['failure_kind'], '')

    def test_actual_abbreviated_list_title_is_not_an_identity_conflict_or_a_full_match(self):
        from backend.services.source_validation import article_samples
        records = [{'url': 'https://example.edu.cn/info/' + str(i) + '.htm', 'title': title}
                   for i, title in enumerate(TITLES)]
        records[0]['title'] = TITLES[0][:16] + '...'
        result = article_samples(dict(ai_column(), list_url=URL), records, self.fixture.fetch)
        self.assertTrue(result['passed'])
        self.assertEqual(result['matched_count'], 2)
        self.assertEqual(result['status'], 'limited')
        self.assertEqual(result['samples'][0]['reason'], 'article_title_abbreviated')
        self.assertFalse(result['samples'][0]['matched'])
        self.assertTrue(result['samples'][0]['title_prefix_observed'])
        records[0]['title'] = '另一个学院不同申请条件与办理流程...'
        result = article_samples(dict(ai_column(), list_url=URL), records, self.fixture.fetch)
        self.assertFalse(result['passed'])
        self.assertEqual(result['samples'][0]['reason'], 'article_identity_conflict')

    def test_onboarding_saves_access_recovery_rules_without_buying_ai_repair(self):
        from backend.scraper.acquisition import FetchResult, FetchFailure
        from backend.services.source_onboarding import onboard_page
        def fetch(address, purpose):
            if address == URL:
                return HTML
            raise FetchFailure(FetchResult(address, status=403, outcome='denied', error_code='http_403'))
        with patch('backend.services.source_onboarding.recognize_column',
                side_effect=AssertionError('403 cannot justify another paid model decision')):
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=fetch)
        self.assertEqual(result['state'], 'access_limited')
        self.assertEqual(result['issues'][0]['codes'], ['article_access_restricted'])
        self.assertEqual(result['recovery_rules'][0]['name'], '通知公告')
        self.assertEqual(Department.query.count(), 0)
        self.assertEqual(self.latest_validation()['status'], 'access_limited')

    def test_restricted_column_does_not_block_neighbor_or_enter_its_rule_repair_prompt(self):
        from backend.scraper.acquisition import FetchResult, FetchFailure
        from backend.services.source_onboarding import onboard_page
        other = '<section id="other"><h2>学术活动</h2><ul>' + ''.join(
            f'<li><a href="/other/{i}.htm">{title}</a><span>2026-10-01</span></li>'
            for i, title in enumerate(TITLES)) + '</ul></section>'
        html = HTML.replace('</body>', other + '</body>')
        columns = [ai_column(), ai_column(name='学术活动', list_selector='#other li',
            container_selector='#other', heading_selector='#other h2'), ai_column(name='未定位区域',
                list_selector='#missing li', container_selector='#missing', heading_selector='#missing h2')]
        reads = []
        def fetch(address, purpose):
            reads.append(address)
            if address == URL:
                return html
            if '/info/' in address:
                raise FetchFailure(FetchResult(address, status=403, outcome='denied', error_code='http_403'))
            return self.fixture.fetch(address, purpose)
        def repair(*args, **kwargs):
            self.assertTrue(kwargs['feedback'])
            self.assertTrue(all('article_access_restricted' not in f['codes'] for f in kwargs['feedback']))
            self.assertTrue(all(f['name'] == '未定位区域' for f in kwargs['feedback']))
            # Repeat both observed valid columns: the blocked trial must retain
            # its recovery state without another read or identity merge.
            return {'status': 'ready', 'columns': columns[:2], 'decision_ids': ['repair-other-region']}
        with patch('backend.services.source_onboarding.publication_lists', return_value=columns), \
                patch('backend.services.source_onboarding.recognize_column', side_effect=repair) as model:
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Department.query.one().name, '学术活动')
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(len([url for url in reads if '/info/' in url]), 3)
        self.assertEqual(result['recovery_rules'][0]['name'], '通知公告')
        self.assertEqual(result['unresolved_rules'][0]['name'], '未定位区域')
        self.assertEqual(model.call_count, 1)

    def test_restricted_pagination_retains_config_and_never_buys_ai_repair(self):
        from backend.scraper.acquisition import FetchResult, FetchFailure
        from backend.services.source_onboarding import onboard_page
        html = HTML.replace('</section>', '<a href="?page=2" rel="next">下一页</a></section>')
        def fetch(address, purpose):
            if address == URL:
                return html
            raise FetchFailure(FetchResult(address, status=403, outcome='denied', error_code='source_login_required'))
        with patch('backend.services.source_onboarding.recognize_column',
                side_effect=AssertionError('A login wall cannot be fixed by a CSS decision')):
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=fetch)
        self.assertEqual(result['state'], 'access_limited')
        self.assertEqual(result['issues'][0]['codes'], ['pagination_access_restricted'])
        self.assertTrue(result['recovery_rules'])
        self.assertEqual(Department.query.count(), 0)

    def test_daily_collection_does_not_ingest_under_an_article_headline(self):
        from types import SimpleNamespace
        from backend.services.source_collection import collect_source
        self.run_page()
        source = Department.query.one()
        source.name = '武汉大学2026年招收攻读博士学位研究生章程'
        source.list_selector = '.yz-list-txt li'
        source.title_selector = source.link_selector = 'a'
        source.date_selector = 'time'
        db.session.commit()
        html = ('<div class="yz-list-box"><div class="yz-tit-img"><a href="/info/1008/7353.htm">'
                + source.name + '</a></div><div class="yz-list-txt"><ul>' + ''.join(
                f'<li><a href="/info/1008/{7963-i}.htm">新增的真实学生通知{i}</a><time>2026-06-03</time></li>'
                for i in range(3)) + '</ul></div></div>')
        with patch('backend.scraper.engine._fetch_html', return_value=html), \
                patch('backend.scraper.selector_monitor.queue_parser_repair',
                      return_value=SimpleNamespace(id=1000, state='pending')) as repair:
            result = collect_source(source)
        self.assertEqual(result['state'], 'repairing')
        self.assertEqual(Announcement.query.count(), 3)
        repair.assert_called_once()
        source.name = '博士生招生'
        db.session.commit()
        with patch('backend.scraper.engine._fetch_html', return_value=html), \
                patch('backend.scraper.selector_monitor.queue_parser_repair',
                      side_effect=AssertionError('A neighboring article must not block a correctly named source')):
            result = collect_source(source)
        self.assertEqual(result['new_count'], 3)
        self.assertEqual(Announcement.query.count(), 6)

    def test_featured_article_link_cannot_name_its_neighboring_notice_list(self):
        from backend.services.source_governance import _column_scope
        title = '武汉大学2026年招收攻读博士学位研究生章程'
        html = ('<div class="yz-list-box"><div class="yz-tit-img"><a href="/info/1008/7353.htm">'
                + title + '</a></div><div class="yz-list-txt"><ul>' + ''.join(
                f'<li><a href="/info/1008/{7963-i}.htm">{text}</a><time>2026-06-03</time></li>'
                for i,text in enumerate(TITLES)) + '</ul></div></div>')
        config = dict(ai_column(), list_url=URL, name=title, list_selector='.yz-list-txt li',
                      title_selector='a', link_selector='a', date_selector='time')
        with patch('backend.services.source_governance.read_snapshot', return_value=html):
            records, errors, scope = _column_scope(config, {'url': URL},
                {'container_selector': '.yz-list-box', 'heading_selector': '.yz-tit-img a'})
        self.assertIn('column_identity_or_scope_unconfirmed', errors)
        self.assertEqual(len(records), 3)

    def test_unrecognized_completed_body_is_retained_for_ai_repair(self):
        from backend.scraper.acquisition import FetchResult, FetchFailure
        from backend.services.source_validation import article_samples
        from backend.services.source_governance import read_snapshot
        material = '<h1>关于学生奖学金申请的通知</h1><div class="custom-body">实际页面材料</div>'
        address = URL + '/a.htm'
        def fetch(*args):
            raise FetchFailure(FetchResult(address, status=200, html=material, outcome='needs_adapter',
                                           error_code='content_not_recognized'))
        result = article_samples({'list_url': URL}, [{'url': address, 'title': '关于学生奖学金申请的通知'}], fetch)
        self.assertFalse(result['passed'])
        sample = result['samples'][0]
        self.assertEqual(sample['error_code'], 'content_not_recognized')
        self.assertEqual(read_snapshot(sample['snapshot']), material)
        self.assertEqual(sample['snapshot']['outcome'], 'needs_adapter')

    def test_validator_upgrade_reuses_failed_checkpoint_without_repeating_ai(self):
        import hashlib
        from backend.services import tasks
        from backend.services.source_onboarding import onboard_page
        from backend.services.source_validation import VALIDATION_VERSION
        job = tasks.enqueue('onboard', 'validation-upgrade',
            {'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False})
        job.checkpoint = {'saved_model_observation': {'cursor': 4}, 'column_validation': {
            'page_hash': hashlib.sha256((URL + '\\0' + HTML).encode()).hexdigest(),
            'validation_version': VALIDATION_VERSION - 1, 'stage': 'done',
            'department_ids': [], 'completed': [], 'new_count': 0,
            'failures': [{'issue': {'codes': ['article_sample_mismatch']}}],
            'result': {'state': 'unsupported'}, 'progress': {'stopped': True}}}
        db.session.commit()
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), patch('backend.services.source_onboarding.recognize_column',
                side_effect=AssertionError('A program validation correction must reuse existing evidence')):
            result = onboard_page(handle['payload'], fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(handle['checkpoint']['saved_model_observation'], {'cursor': 4})
        self.assertEqual(handle['checkpoint']['column_validation']['validation_version'], VALIDATION_VERSION)

    def test_article_metadata_identifies_a_real_body_instead_of_an_access_wall(self):
        from backend.services.source_validation import article_samples
        title = '关于开展学生奖学金申请工作的通知'
        html = ('<meta name="ArticleTitle" content="' + title + '"><h1>' + title + '</h1>'
                '<div class="v_news_content"><p>各位同学，请按学校公布的条件提交奖学金申请。'
                '学院将在审核后公布结果，请关注网站发布的信息，及时提交申请材料。'
                '本次申请须按照通知所列的要求办理，具体时间请以学校安排为准。</p></div>')
        result = article_samples({'list_url': URL, 'content_selector': '.v_news_content'},
            [{'url': URL + '/a.htm', 'title': title}], lambda *_: html)
        self.assertTrue(result['passed'])

    def test_executed_article_region_in_aside_is_preserved_but_sidebar_is_rejected(self):
        from backend.services.source_validation import article_samples
        title = '关于开展学生奖学金申请工作的通知'
        article = '<h3>' + title + '</h3><div class="article-body"><p>各位同学，请按学校公布的条件提交奖学金申请，学院将在审核后公布结果。</p></div>'
        for marker, expected in (('gp-subRight', True), ('sidebar', False)):
            html = '<aside class="' + marker + '">' + article + '</aside>'
            result = article_samples({'list_url': URL, 'content_selector': '.article-body'},
                [{'url': URL + '/a.htm', 'title': title}], lambda *_: html)
            self.assertEqual(result['passed'], expected)

    def test_direct_pdf_notice_requires_verified_content_and_matching_attachment_identity(self):
        from backend.scraper.acquisition import FetchResult, FetchedHTML
        from backend.services.source_validation import article_samples
        title = '关于开展学生奖学金申请工作的通知'
        records = [{'title': title, 'url': 'https://example.edu.cn/files/notice.pdf'}]
        for filename, matched in ((title + '.pdf', True), ('另一个栏目文件.pdf', False), ('ab132980aabf45c63265.pdf', True)):
            result = FetchResult(records[0]['url'], status=200, outcome='usable', attachment={
                'format': 'pdf', 'verified': True, 'filename': filename, 'bytes': 500,
                'sha256': 'a' * 64})
            samples = article_samples({'list_url': URL}, records, lambda *_: FetchedHTML(result))
            self.assertEqual(samples['passed'], matched)
            self.assertEqual(samples['samples'][0]['attachment'], True)

    def test_short_table_cells_form_a_real_notice_body(self):
        from backend.services.source_validation import article_samples
        title = '各学院招生申请办法及复试细则汇总'
        html = '<p>' + title + '</p><table><tr><th>学院</th><th>通知链接</th></tr>' + ''.join(
            f'<tr><td>{name}</td><td><a href="https://example.edu.cn/info/{i}.htm">学院通知</a></td></tr>'
            for i, name in enumerate(['石油工程学院','化工与环境学院','机电工程学院'])) + '</table>'
        result = article_samples({'list_url': URL}, [{'url': URL + '/a.htm', 'title': title}], lambda *_: html)
        self.assertTrue(result['passed'])
        self.assertFalse(result['samples'][0]['attachment'])

    def test_header_named_page_wrapper_must_not_remove_its_article(self):
        from backend.services.source_validation import article_samples
        title = '上海交通大学关于2026年放假及调课安排的通知'
        html = ('<div class="headerpage"><header><a>导航</a></header><div class="mainpage">'
                '<h2 class="Article-title">' + title + '</h2><div class="Article_content">'
                '<p>各单位，学校研究决定，现将放假安排通知如下，请认真落实教学安排。</p>'
                '</div></div><footer>版权所有</footer></div>')
        result = article_samples({'list_url': URL}, [{'url': URL + '/a.htm', 'title': title}], lambda *_: html)
        self.assertTrue(result['passed'])

    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests(); self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def run_page(self, html=HTML, **payload):
        from backend.services.source_onboarding import onboard_page
        return onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False, **payload},
                            fetcher=lambda url, purpose: html if url == URL else self.fixture.fetch(url, purpose))

    def latest_validation(self):
        row = SourceValidation.query.order_by(SourceValidation.id.desc()).first()
        self.assertIsNotNone(row)
        return json.loads(row.validation_json)

    def test_default_reads_three_distinct_articles_and_records_evidence(self):
        result = self.run_page()
        self.assertEqual(result['state'], 'connected')
        reads = [url for url, purpose in self.fixture.reads if purpose == 'article']
        self.assertEqual(len(reads), 3)
        self.assertEqual(len(set(reads)), 3)
        evidence = self.latest_validation()
        self.assertEqual(evidence['status'], 'full')
        self.assertEqual(evidence['articles']['matched_count'], 3)
        self.assertEqual(evidence['region']['name'], '通知公告')

    def test_attachment_landing_page_is_not_installed_from_its_repeated_widget(self):
        html = HTML.replace('<body>', '<body class="single single-attachment"><article id="post-18724" class="type-attachment"><h1>本科教学质量报告</h1><a href="/report.pdf">点击下载</a></article>')
        with patch('backend.services.source_onboarding.publication_lists', return_value=[ai_column()]):
            result = self.run_page(html)
        self.assertEqual(result['state'], 'not_column')
        self.assertEqual(Department.query.count(), 0)
        self.assertFalse(self.fixture.reads)

    def test_literal_bilingual_column_name_keeps_its_slash_and_same_channel_on_repair(self):
        name = '学院新闻 / NEWS'
        html = HTML.replace('通知公告', name)
        column = ai_column(name=name)
        with patch('backend.services.source_onboarding.publication_lists', return_value=[column]):
            first = self.run_page(html)
            second = self.run_page(html, revalidate=True)
        self.assertEqual(first['state'], 'connected')
        self.assertEqual(first['department_ids'], second['department_ids'])
        self.assertEqual(Department.query.one().name, name)

    def test_one_matching_article_out_of_three_does_not_install_rule(self):
        from backend.services.source_onboarding import onboard_page
        def fetch(url, purpose):
            if url == URL:
                return HTML
            if url.endswith('/0.htm'):
                return self.fixture.fetch(url, purpose)
            return '<article><h1>其他无关新闻</h1><p>这是另一个消息，不能证明通知列表和栏目可用。</p></article>'
        result = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False}, fetcher=fetch)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(Department.query.count(), 0)
        self.assertEqual(self.latest_validation()['articles']['matched_count'], 1)

    def test_single_observed_notice_can_only_supply_limited_validation(self):
        html = HTML.replace(''.join(f'<li><a href="/info/{i}.htm">{title}</a><span>2026-10-01</span></li>'
                                   for i, title in enumerate(TITLES) if i), '')
        with patch('backend.services.source_onboarding.publication_lists', return_value=[ai_column()]):
            result = self.run_page(html)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 1)
        self.assertEqual(self.latest_validation()['status'], 'limited')

    def test_empty_official_region_is_neither_connected_nor_failed(self):
        html = '<section id="notices"><h2>通知公告</h2><ul></ul><p>暂无内容</p></section>'
        with patch('backend.services.source_onboarding.publication_lists', return_value=[ai_column()]):
            result = self.run_page(html)
        self.assertEqual(result['state'], 'empty')
        self.assertEqual(result['department_ids'], [])
        self.assertFalse(result['issues'])
        self.assertEqual(self.latest_validation()['status'], 'empty')

    def test_observed_next_page_must_work_without_repeating_first_page(self):
        from backend.services.source_onboarding import onboard_page
        html = HTML.replace('</section>', '<a href="?page=2" rel="next">下一页</a></section>')
        next_url = URL + '?page=2'
        reads = []
        def fetch(url, purpose):
            reads.append(url)
            return html if url in (URL, next_url) else self.fixture.fetch(url, purpose)
        result = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False}, fetcher=fetch)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(reads.count(next_url), 1)
        self.assertEqual(Department.query.count(), 0)
        self.assertIn('pagination_repeats_first_page', result['issues'][0]['codes'])

    def test_selector_change_keeps_id_when_actual_region_and_articles_agree(self):
        first = self.run_page()
        source = db.session.get(Department, first['department_ids'][0])
        html = HTML.replace('<ul>', '<ul class="revised">')
        with patch('backend.services.source_onboarding.publication_lists', return_value=[ai_column(list_selector='#notices .revised li')]):
            second = self.run_page(html, revalidate=True)
        self.assertEqual(second['department_ids'], [source.id])
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(source.list_selector, '#notices .revised li')

    def test_same_page_same_name_new_region_is_a_distinct_source(self):
        first = self.run_page()
        other = '<section id="other"><h2>通知公告</h2><ul>' + ''.join(
            f'<li><a href="/other/{i}.htm">{title}</a><span>2026-10-01</span></li>'
            for i, title in enumerate(TITLES)) + '</ul></section>'
        with patch('backend.services.source_onboarding.publication_lists', return_value=[
                ai_column(list_selector='#other li', container_selector='#other', heading_selector='#other h2')]):
            second = self.run_page(HTML.replace('</body>', other + '</body>'), revalidate=True)
        self.assertEqual(second['state'], 'connected')
        self.assertNotEqual(first['department_ids'], second['department_ids'])
        self.assertEqual(Department.query.count(), 2)

    def test_failed_repair_leaves_current_rule_intact(self):
        first = self.run_page()
        source = db.session.get(Department, first['department_ids'][0])
        old_selector = source.list_selector
        with patch('backend.services.source_onboarding.publication_lists', return_value=[ai_column(list_selector='#missing li')]):
            second = self.run_page(revalidate=True)
        self.assertEqual(second['state'], 'unsupported')
        self.assertEqual(source.list_selector, old_selector)
        self.assertEqual(Department.query.count(), 1)

    def test_sample_fallback_never_reads_more_than_five_unique_articles(self):
        from backend.services.source_validation import article_samples
        records = [{'title': title, 'url': f'https://example.edu.cn/info/{i}.htm'}
                   for i, title in enumerate(TITLES * 2)]
        reads = []
        def fetch(url, purpose):
            reads.append(url)
            return '<article><h1>其他文章</h1><p>这是一个完全不同的通知内容，不是当前列表的文章。</p></article>'
        result = article_samples(dict(ai_column(), list_url=URL), records + records, fetch)
        self.assertFalse(result['passed'])
        self.assertEqual(len(reads), 5)
        self.assertEqual(len(set(reads)), 5)

    def test_title_in_sidebar_does_not_prove_article_identity(self):
        from backend.services.source_validation import article_samples
        records = [{'title': TITLES[0], 'url': 'https://example.edu.cn/info/0.htm'}]
        html = f'<article><h1>其他消息</h1><p>这是另一个栏目里的文章，不是提取列表所指向的这条通知。</p></article><aside><a>{TITLES[0]}</a></aside>'
        result = article_samples(dict(ai_column(), list_url=URL), records, lambda *_: html)
        self.assertFalse(result['passed'])

    def test_plain_cms_title_is_real_article_identity_without_heading_tag(self):
        from backend.services.source_validation import article_samples
        records = [{'title': TITLES[0], 'url': 'https://example.edu.cn/info/0.htm'}]
        html = f'<title>大学教务部</title><div class="content"><p>{TITLES[0]}</p><p>请各学院组织学生按照教学计划完成课程选课并确认个人课表。</p></div>'
        result = article_samples(dict(ai_column(content_selector='.content'), list_url=URL), records, lambda *_: html)
        self.assertTrue(result['passed'])
        self.assertEqual(result['status'], 'limited')

    def test_single_first_page_with_real_later_notice_is_fully_sampled(self):
        from backend.services.source_onboarding import onboard_page
        first = '<section id="notices"><h2>通知公告</h2><ul><li><a href="/info/0.htm">' + TITLES[0] + '</a></li></ul><a href="?page=2" rel="next">下一页</a></section>'
        later = '<section id="notices"><h2>通知公告</h2><ul><li><a href="/info/1.htm">' + TITLES[1] + '</a></li></ul></section>'
        def fetch(url, purpose):
            return first if url == URL else later if url == URL + '?page=2' else self.fixture.fetch(url, purpose)
        with patch('backend.services.source_onboarding.publication_lists', return_value=[ai_column(date_selector='')]):
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'ai_assist': False}, fetcher=fetch)
        self.assertEqual(result['state'], 'connected')
        evidence = self.latest_validation()
        self.assertEqual(evidence['status'], 'full')
        self.assertEqual(evidence['articles']['matched_count'], 2)
        self.assertEqual(evidence['pagination']['state'], 'validated')

    def test_distinct_failed_decisions_stop_after_two_no_progress_windows(self):
        from backend.services.source_onboarding import onboard_page
        count = 0
        def recognize(*args, **kwargs):
            nonlocal count
            count += 1
            return {'status': 'ready', 'decision_ids': [f'trial-{count}'],
                    'columns': [ai_column(list_selector=f'#missing-{count} li')]}
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=recognize):
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(result['error_code'], 'no_progress')
        self.assertEqual(count, 8)

    def test_budget_wait_preserves_same_task_stage_and_feedback(self):
        from backend.services import tasks
        from backend.services.source_onboarding import onboard_page
        job = tasks.enqueue('onboard', 'budget-proof', {'school_id': self.fixture.school_id, 'url': URL})
        handle = tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle), \
             patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', return_value={
                 'status': 'unsupported', 'columns': [], 'error_code': 'budget_exhausted'}):
            with self.assertRaises(tasks.TaskDeferred) as raised:
                onboard_page(job.payload, fetcher=self.fixture.fetch)
        deferred = raised.exception
        self.assertEqual((deferred.state, deferred.phase), ('waiting', 'ai_budget'))
        self.assertEqual(deferred.checkpoint['column_validation']['stage'], 'ai')
        self.assertEqual(job.generation, 1)

    def test_peer_headings_without_separate_wrappers_cannot_mix_columns(self):
        from backend.services.source_governance import _column_scope
        html = ('<section id="columns"><h2 id="notices">通知公告</h2><ul><li><a href="/info/0.htm">'
                + TITLES[0] + '</a></li></ul><h2>校园新闻</h2><ul><li><a href="/info/1.htm">'
                + TITLES[1] + '</a></li></ul></section>')
        for markup, heading in ((html, '#notices'), (html.replace('<h2', '<div class="column-label"').replace('</h2>', '</div>'), '#notices')):
            with self.subTest(custom_heading='column-label' in markup), \
                 patch('backend.services.source_governance.read_snapshot', return_value=markup):
                _, errors, _ = _column_scope(dict(ai_column(list_selector='#columns li'), list_url=URL),
                    {'url': URL}, {'container_selector': '#columns', 'heading_selector': heading})
            self.assertIn('column_identity_or_scope_unconfirmed', errors)

    def test_current_page_breadcrumb_can_name_its_single_adjacent_list(self):
        from backend.services.source_governance import _column_scope
        listing = '<ul>' + ''.join(f'<li><a href="/info/{i}.htm">{title}</a></li>' for i, title in enumerate(TITLES)) + '</ul>'
        markup = ('<main><div class="crumb"><a href="/">首页</a> / <a href="' + URL + '">通知公告</a></div>'
                  + '<div class="listing">' + listing + '</div></main>')
        config = dict(ai_column(list_selector='.listing li', date_selector=''), list_url=URL)
        for html, target, passed in ((markup, URL, True), (markup, URL + 'other/', False),
                (markup.replace('</main>', '<h2>校园新闻</h2>' + listing + '</main>'), URL, False)):
            with self.subTest(passed=passed, target=target), \
                 patch('backend.services.source_governance.read_snapshot', return_value=html), \
                 patch('backend.scraper.discovery.publication_lists.publication_lists', return_value=[]):
                records, errors, region = _column_scope(dict(config, list_url=target), {'url': target},
                    {'container_selector': '.listing', 'heading_selector': '.crumb'})
            self.assertEqual(not errors, passed)
            if passed:
                self.assertEqual(region['name'], '通知公告')
                self.assertEqual(len(records), 3)

    def test_partial_ai_region_installs_before_resume_and_keeps_model_input(self):
        from backend.services import tasks
        from backend.services.source_onboarding import onboard_page
        job = tasks.enqueue('onboard', 'partial-regions', {'school_id': self.fixture.school_id, 'url': URL})
        handle = tasks.claim(capabilities=['http'])
        first = {'status': 'ready', 'columns': [ai_column()], 'decision_ids': ['region-one'],
                 'material_complete': False, 'remaining_regions': 1}
        with tasks.execution_scope(handle), \
             patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', return_value=first):
            with self.assertRaises(tasks.TaskDeferred) as deferred:
                onboard_page(job.payload, fetcher=self.fixture.fetch)
        self.assertEqual(Announcement.query.count(), 3)
        original = Department.query.one().id
        self.assertEqual(deferred.exception.phase, 'source_material')
        tasks.handoff(handle, deferred.exception)
        db.session.remove()
        resumed = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(resumed), \
             patch('backend.services.source_onboarding.recognize_column', return_value={
                 **first, 'decision_ids': ['region-one', 'region-two'], 'material_complete': True,
                 'remaining_regions': 0}) as recognize:
            result = onboard_page(resumed['payload'], fetcher=self.fixture.fetch)
        self.assertEqual(result['department_ids'], [original])
        self.assertEqual(result['new_count'], 3)
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(SourceValidation.query.filter_by(department_id=original).count(), 1)
        self.assertIsNone(recognize.call_args.kwargs['feedback'])
        self.assertEqual(resumed['checkpoint']['column_validation']['progress']['decisions'], 2)

    def test_composite_tab_strip_cannot_name_one_tab_as_all_channels(self):
        from backend.services.source_governance import _column_scope
        name = '招聘信息基层招聘信息医科招聘信息实习生招聘信息'
        markup = '<section id="jobs"><div class="btitle" id="tabs">' + ''.join(
            f'<div><div onclick="location.href=\'/type-{i}\'" onmouseover="Tabs(this)">{label}</div></div>'
            for i, label in enumerate(['招聘信息', '基层招聘信息', '医科招聘信息', '实习生招聘信息']))
        markup += '</div><ul id="internships">' + ''.join(
            f'<li><a href="/info/{i}.htm">{title}</a></li>' for i, title in enumerate(TITLES)) + '</ul></section>'
        config = dict(ai_column(name=name, list_selector='#internships li', date_selector=''), list_url=URL)
        feed = dict(config, heading_locator='#tabs', container_locator='#internships')
        for scope in (None, {'container_selector': '#jobs', 'heading_selector': '#tabs'}):
            with self.subTest(scope=scope), patch('backend.services.source_governance.read_snapshot', return_value=markup), \
                 patch('backend.scraper.discovery.publication_lists.publication_lists', return_value=[feed]):
                _, errors, _ = _column_scope(config, {'url': URL}, scope)
            self.assertIn('column_identity_or_scope_unconfirmed', errors)

    def test_empty_body_cannot_borrow_navigation_footer_or_duplicate_title(self):
        from backend.services.source_validation import article_samples
        title = TITLES[0]
        markup = (f'<title>{title}</title><h1>{title}</h1><div class="page-content"><p>{title}</p></div>'
            '<div class="site-navigation"><p>学生服务办事指南与各种常用材料下载</p><a href="/forms/profile.pdf">个人材料下载</a></div>'
            '<div class="site-footer"><p>学校地址校园路一号，联系电话与网站备案信息详见学校首页</p></div>')
        for selector in ('', 'body', '.page-content'):
            with self.subTest(selector=selector):
                result = article_samples(dict(ai_column(content_selector=selector), list_url=URL),
                    [{'title': title, 'url': URL + 'empty.htm'}], lambda *_: markup)
            self.assertFalse(result['passed'])
            self.assertFalse(result['samples'][0]['attachment'])

    def test_real_cupk_pdf_notice_without_configured_body_keeps_attachment_evidence(self):
        from backend.services.source_validation import article_samples
        # Literal title and attachment fragment from the independently fetched
        # /jwb/c/2026-05-25/534468.shtml snapshot, not invented sample content.
        title = '中国石油大学（北京）克拉玛依校区2026-2027学年校历'
        attachment = '/jwb/upload/resources/file/2026/05/25/113142.pdf'
        markup = (f'<title>中国石油大学（北京）克拉玛依校区教务部</title><div><p>{title}</p></div>'
            f'<div><a filerela="113142" href="{attachment}" target="_blank" title="{title}">'
            f'<img src="/jwb/images/filetype/pdf.gif" title="pdf"/> {title}</a></div>')
        result = article_samples({'content_selector': '', 'list_url': 'https://www.cupk.edu.cn/jwb/jxyx/xl/'},
            [{'title': title, 'url': 'https://www.cupk.edu.cn/jwb/c/2026-05-25/534468.shtml'}], lambda *_: markup)
        self.assertTrue(result['passed'])
        proof = result['samples'][0]
        self.assertTrue(proof['attachment'])
        self.assertIn('https://www.cupk.edu.cn' + attachment, proof['attachment_urls'])
