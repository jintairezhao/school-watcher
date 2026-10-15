"""Observed list titles identify custom static article bodies before rendering."""
import gzip
from pathlib import Path
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
from backend.scraper.article_observation import title_bound_body


URL = 'https://www.cupk.edu.cn/library/c/2026-09-24/538770.shtml'
TITLE = '原点参数一站式AI数据资源库试用通知'
# Reduced from the retained real 2026-10-15 library article observation. The
# publication metadata and prose share a container beside the nested headline.
HTML = ('<html><title>中国石油大学(北京)克拉玛依校区图书馆</title><body>'
        '<div class="c-main__right"><div class="c-right-detail">'
        '<div class="c-right-detail__header"><div class="c-right-detail__title">'
        f'<div class="c-right-detail__title-name">{TITLE}</div></div></div></div>'
        '<div class="c-right-detail__content">'
        '<div>发布日期：2026-09-24 作者： 来源：07-图书馆 浏览：67</div>'
        '<p>访问方式：校园网IP范围内</p>'
        '<p>原点参数为高校科研人员、学生开发一站式标准化数据接口服务，'
        '支持 AI 智能体（Agent）通过自然语言、程序代码直接调用，'
        '有效降低社科、金融领域的数据获取与技术对接门槛。</p>'
        '<p>数据接口覆盖宏观统计、政策文本、产业研究，以及A股、港股、美股、日股、台股等经济金融多维数据资源，'
        '兼顾时序数值数据与政策、公告类文本数据；配套 Skill、Python SDK、Restful HTTP 多种调用模式，'
        '可对接各类 AI 智能体与大模型开发工具，为大模型应用、智能体工具调用、时序预测、课题实证研究'
        '提供稳定可靠的数据底座。</p></div></div>'
        '<aside><p>其他推荐资源与学校服务链接不能当作通知正文。</p></aside>'
        '</body></html>')

MAIN_URL = 'https://www.cupk.edu.cn/c/2026-07-03/535091.shtml'
MAIN_TITLE = '第九届油气成藏机理与油气资源评价国际学术研讨会（二号通知）'
# The live main-site read retains compact publication dates until its browser
# scripts format them. This is actual metadata, independent of that script.
MAIN_HTML = ('<div class="c-right-detail"><div class="c-right-detail__header">'
             f'<div class="c-right-detail__title"><div class="c-right-detail__title-name1">{MAIN_TITLE}</div></div></div>'
             '<div class="SecondList__msg">发布日期：20260703 作者：石油学院 来源：石油学院 '
             '编辑：官网管理员 浏览：<span>1659</span></div>'
             '<div class="c-right-detail__content"><p>为进一步交流常规与非常规油气成藏机理、'
             '油气资源评价等方面的新理论、新方法与新技术，现将有关会议安排通知如下。</p></div></div>')

YJSB_URL = 'https://www.cupk.edu.cn/yjsb/c/2026-10-08/539002.shtml'
YJSB_TITLE = '2027年全国硕士研究生招生考试中国石油大学（北京）克拉玛依校区（6534）报考点公告'
YJSB_HTML = gzip.decompress((Path(__file__).parent / 'fixtures/acquisition/cupk_yjsb_20261008.html.gz').read_bytes()).decode()


class ExploratoryArticleMaterialTests(unittest.TestCase):
    def test_complete_real_yjsb_header_and_adjacent_body_need_no_render(self):
        soup = BeautifulSoup(YJSB_HTML, 'lxml')
        observed_body = soup.select_one('div.middleIntroduce > div:nth-of-type(2)')
        self.assertGreater(len(observed_body.get_text(' ', strip=True)), 3500)
        body = title_bound_body(soup, YJSB_URL, YJSB_TITLE)
        self.assertIsNotNone(body)
        self.assertIn(observed_body.get_text(' ', strip=True), body.get_text(' ', strip=True))
        result = classify_result(FetchRequest(YJSB_URL, purpose='article',
            policy={'expected_article_title': YJSB_TITLE}), FetchResult(YJSB_URL, status=200, html=YJSB_HTML))
        self.assertTrue(result.ok, result.error_code)
        self.assertIn('title_bound_article', result.evidence)

    def test_verified_real_yjsb_explicit_selector_precedes_missing_body_guess(self):
        result = classify_result(FetchRequest(YJSB_URL, purpose='article',
            readiness_selector='div.middleIntroduce div.u-clearfix + div',
            policy={'expected_article_title': YJSB_TITLE}), FetchResult(YJSB_URL, status=200, html=YJSB_HTML))
        self.assertTrue(result.ok, result.error_code)
        self.assertIn('readiness_selector', result.evidence)

    def test_separate_header_keeps_adjacent_short_prose_and_attachments(self):
        for content in ('<div><p>请同学们按照学校正式公布的时间和要求办理本次相关手续。</p></div>',
                        '<p>请同学们按照学校正式公布的时间和要求办理本次相关手续。</p>',
                        '<div><a href="/documents/notice.pdf">正式通知附件</a></div>'):
            with self.subTest(content=content):
                html = f'<main><header><h1>{TITLE}</h1><div>发布日期：2026-09-24</div></header>{content}</main>'
                self.assertIsNotNone(title_bound_body(BeautifulSoup(html, 'lxml'), URL, TITLE))

    def test_adjacent_header_fallback_never_crosses_an_article_or_material_boundary(self):
        prose = '<p>这是另一段很长的页面材料，不能借给当前缺少正文的通知作为成功依据。</p>'
        cases = {
            'different_article': '<section><h2>另一条通知标题</h2><div>发布日期：2026-09-25</div>' + prose + '</section>',
            'plain_other_article': '<div><p>这是另一条完整通知的独立标题</p><div>发布时间：2026-09-25</div>' + prose + '</div>',
            'heading_boundary': '<h2>另一栏目</h2><div>' + prose + '</div>',
            'hidden_then_other': '<div hidden>' + prose + '</div><div>' + prose + '</div>',
            'pending_then_other': '<div aria-busy="true">' + prose + '</div><div>' + prose + '</div>',
            'loading_then_other': '<div><p>正文加载中，请稍候，正在获取本条通知的详细信息。</p></div><div>' + prose + '</div>',
            'navigation_then_other': '<nav>' + prose + '</nav><div>' + prose + '</div>',
            'header_only': '<header>' + prose + '</header>',
            'aside_logo': '<div><aside><img src="/images/logo.svg"/></aside></div>',
            'footer_only': '<div><footer>' + prose + '</footer></div>',
            'nested_class_footer': '<div><div class="footer">' + prose + '</div></div>',
            'nested_class_header': '<div><div class="header">' + prose + '</div></div>',
            'nested_class_sidebar_logo': '<div><div class="sidebar"><img src="/images/logo.svg"/></div></div>',
        }
        for case, content in cases.items():
            with self.subTest(case=case):
                html = f'<main><header><h1>{TITLE}</h1><div>发布日期：2026-09-24</div></header>{content}</main>'
                self.assertIsNone(title_bound_body(BeautifulSoup(html, 'lxml'), URL, TITLE))

    def test_body_chrome_is_removed_without_removing_identity_or_short_ready_material(self):
        widgets = ('<div class="site-header"><p>全站欢迎与导航说明不属于通知正文。</p></div>'
                   '<div id="site_footer"><p>全站版权与联系说明不属于通知正文。</p></div>'
                   '<div class="sidebar"><img src="/images/logo.svg"/></div>')
        for content in ('<p>请同学们按照学校正式公布的时间和要求办理本次相关手续。</p>',
                        '<a href="/documents/notice.pdf">正式通知附件</a>'):
            with self.subTest(content=content):
                html = (f'<main><div class="article-header"><h1>{TITLE}</h1>'
                    '<div>发布日期：2026-09-24</div></div>'
                    f'<div class="headerpage body-content">{widgets}{content}</div></main>')
                soup = BeautifulSoup(html, 'lxml')
                body = title_bound_body(soup, URL, TITLE)
                self.assertIsNotNone(body)
                self.assertNotIn('全站', body.get_text())
                self.assertIsNone(body.select_one('img'))
                self.assertEqual(soup.select_one('.article-header h1').get_text(), TITLE)
                if 'notice.pdf' in content:
                    self.assertIsNotNone(body.select_one('a[href="/documents/notice.pdf"]'))

    def test_header_fallback_stops_at_its_one_local_parent(self):
        prose = '<div><p>无关页面介绍和推荐栏目不能借给只有标题日期的文章区域。</p></div>'
        for html in (
            f'<body><header><h1>{TITLE}</h1><div>发布日期：2026-09-24</div></header>{prose}</body>',
            f'<main><article><h1>{TITLE}</h1><div>发布日期：2026-09-24</div></article>{prose}</main>',
            f'<main><section><header><h1>{TITLE}</h1><div>发布日期：2026-09-24</div></header></section>{prose}</main>',
        ):
            with self.subTest(html=html):
                self.assertIsNone(title_bound_body(BeautifulSoup(html, 'lxml'), URL, TITLE))

    def test_a_complete_local_body_does_not_expand_outside_its_scope(self):
        html = (f'<main><section><h1>{TITLE}</h1><div>发布日期：2026-09-24</div>'
            '<div><p>这是当前通知已经明确取得的真实正文，需要完整保留本段材料。</p></div></section>'
            '<div><p>外部版权和推荐介绍不属于该通知的实际正文，绝不能混入。</p></div></main>')
        body = title_bound_body(BeautifulSoup(html, 'lxml'), URL, TITLE)
        self.assertIsNotNone(body)
        self.assertNotIn('外部版权', body.get_text())

    def test_pending_adjacent_body_renders_then_accepts_actual_ready_content(self):
        for attribute in ('aria-busy="true"', ''):
            content = ('<p>正文加载中，请稍候，正在获取本条通知的详细信息。</p>' if not attribute
                else '<p>已经收到一部分实际正文，仍在等待网页完成最后的数据加载。</p>')
            pending = (f'<main><header><h1>{TITLE}</h1><div>发布日期：2026-09-24</div></header>'
                       f'<div {attribute}>{content}</div></main>')
            request = FetchRequest(URL, purpose='article', policy={'expected_article_title': TITLE})
            result = classify_result(request, FetchResult(URL, status=200, html=pending))
            self.assertEqual(result.outcome, 'requires_render')
        ready = pending.replace('正文加载中，请稍候，正在获取本条通知的详细信息。',
                                '请同学们按照学校正式公布的时间和要求办理本次相关手续。')
        self.assertTrue(classify_result(request, FetchResult(URL, status=200, html=ready, transport='browser')).ok)

    def test_metadata_inside_the_body_container_does_not_hide_real_prose(self):
        body = title_bound_body(BeautifulSoup(HTML, 'lxml'), URL, TITLE)
        self.assertIsNotNone(body)
        self.assertIn('高校科研人员、学生', body.get_text())
        self.assertNotIn('其他推荐资源', body.get_text())

    def test_observed_title_recognizes_static_custom_body_without_browser(self):
        request = FetchRequest(URL, purpose='article', policy={'expected_article_title': TITLE})
        result = classify_result(request, FetchResult(URL, status=200, html=HTML))
        self.assertTrue(result.ok)
        self.assertIn('title_bound_article', result.evidence)

    def test_explicit_body_selector_still_has_to_match(self):
        # Use separate metadata/body siblings so the existing title fallback
        # already recognizes this layout; it must not override an explicit rule.
        html = HTML.replace('<div class="c-right-detail__content"><div>',
                            '<div>').replace('浏览：67</div>',
                            '浏览：67</div><div class="c-right-detail__content">')
        request = FetchRequest(URL, purpose='article', readiness_selector='.reviewed-body',
                               policy={'expected_article_title': TITLE})
        result = classify_result(request, FetchResult(URL, status=200, html=html))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'readiness_missing')

    def test_missing_body_or_different_title_cannot_borrow_site_prose(self):
        for case in ('missing_body', 'wrong_title'):
            with self.subTest(case=case):
                soup = BeautifulSoup(HTML, 'lxml')
                if case == 'missing_body':
                    for node in soup.select('.c-right-detail__content p'):
                        node.decompose()
                request = FetchRequest(URL, purpose='article',
                    policy={'expected_article_title': TITLE if case == 'missing_body' else '另一条通知'})
                result = classify_result(request, FetchResult(URL, status=200, html=str(soup)))
                self.assertFalse(result.ok)
                self.assertEqual(result.outcome, 'needs_adapter')

    def test_short_body_and_metadata_inside_one_wrapper_remain_distinct(self):
        soup = BeautifulSoup(HTML, 'lxml')
        for node in soup.select('.c-right-detail__content p'):
            node.decompose()
        prose = soup.new_tag('p')
        prose.string = '请同学们按照学校正式通知公布的时间和要求办理相关手续。'
        soup.select_one('.c-right-detail__content').append(prose)
        body = title_bound_body(soup, URL, TITLE)
        self.assertIsNotNone(body)
        self.assertIn(prose.get_text(), body.get_text())

    def test_default_sample_reader_binds_the_observed_title_without_guessing_body_selector(self):
        from backend.services.source_onboarding import _read
        with patch('backend.scraper.acquisition.coordinator.http_fetch',
                   return_value=FetchResult(URL, status=200, html=HTML)) as http, \
             patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch',
                   side_effect=AssertionError('The body is already in the static DOM')):
            document = _read(URL, 'article', config={'content_selector': ''}, exploration=True,
                             article_titles={URL: TITLE})
        self.assertTrue(document.result.ok)
        self.assertEqual(http.call_args.args[0].policy['expected_article_title'], TITLE)
        self.assertEqual(http.call_args.args[0].readiness_selector, '')

    def test_inferred_list_structure_does_not_claim_unobserved_article_css(self):
        from backend.scraper.discovery.publication_lists import publication_lists
        listing = '<section><h2>最新资源</h2><ul class="resources">' + ''.join(
            f'<li class="notice"><a href="/library/c/2026-09-24/{538770 + n}.shtml">'
            f'关于公开资源试用安排的通知{n}</a><time>2026-09-24</time></li>' for n in range(4)) + '</ul></section>'
        inferred = [config for config in publication_lists(listing, 'https://www.cupk.edu.cn/library/')
                    if config['method'] == 'dom_analysis']
        self.assertTrue(inferred)
        self.assertTrue(all(config['content_selector'] == '' for config in inferred))

    def test_explicit_compact_publication_line_identifies_actual_main_site_body(self):
        from backend.services.publication_dates import inspect_article_date
        result = classify_result(FetchRequest(MAIN_URL, purpose='article',
            policy={'expected_article_title': MAIN_TITLE}), FetchResult(MAIN_URL, status=200, html=MAIN_HTML))
        self.assertTrue(result.ok)
        proof = inspect_article_date(MAIN_HTML, MAIN_URL, MAIN_TITLE)
        self.assertEqual(proof['state'], 'verified')
        self.assertEqual(proof['date'], '2026-07-03')
        self.assertIn('发布日期：20260703', proof['text'])

    def test_plain_compact_numbers_event_dates_and_invalid_dates_are_not_publication_metadata(self):
        from backend.services.publication_dates import inspect_article_date
        for replacement in ('编号：20260703', '活动日期：20260703', '发布日期：20260230',
                            '发布日期：20260703 发布日期：20260704'):
            with self.subTest(metadata=replacement):
                html = MAIN_HTML.replace('发布日期：20260703', replacement)
                request = FetchRequest(MAIN_URL, purpose='article', policy={'expected_article_title': MAIN_TITLE})
                self.assertFalse(classify_result(request, FetchResult(MAIN_URL, status=200, html=html)).ok)
                self.assertEqual(inspect_article_date(html, MAIN_URL, MAIN_TITLE)['state'], 'unknown')

    def test_missing_main_body_cannot_borrow_an_adjacent_notice_with_its_own_date(self):
        soup = BeautifulSoup(MAIN_HTML, 'lxml')
        soup.select_one('.c-right-detail__content').clear()
        other = BeautifulSoup('<section><h2>另一条完整通知</h2><div>发布日期：20260704</div>'
            '<p>这些是相邻另一条通知的正文内容，不能作为当前标题已经取得正文的依据。</p></section>', 'lxml').section
        soup.select_one('.c-right-detail').append(other)
        self.assertIsNone(title_bound_body(soup, MAIN_URL, MAIN_TITLE))

    def test_hidden_and_pending_body_material_never_counts_as_a_ready_article(self):
        for contract in ('title_bound', 'article_region', 'explicit_selector'):
            for state in ('hidden', 'busy', 'placeholder'):
                with self.subTest(contract=contract, state=state):
                    soup = BeautifulSoup(MAIN_HTML, 'lxml')
                    region = soup.select_one('.c-right-detail__content')
                    region['id'] = 'body'
                    if contract == 'article_region':
                        region.name = 'article'
                    if state == 'hidden':
                        region['hidden'] = ''
                    elif state == 'busy':
                        region['aria-busy'] = 'true'
                    else:
                        region.clear()
                        paragraph = soup.new_tag('p')
                        paragraph.string = '正文加载中，请稍候，正在获取本条通知的详细信息。'
                        region.append(paragraph)
                    request = FetchRequest(MAIN_URL, purpose='article',
                        readiness_selector='#body' if contract == 'explicit_selector' else '',
                        policy={'expected_article_title': MAIN_TITLE} if contract == 'title_bound' else {})
                    result = classify_result(request, FetchResult(MAIN_URL, status=200,
                        html=str(soup) + '<script>fetch("/api/article")</script>'))
                    self.assertFalse(result.ok)

    def test_a_ready_body_is_accepted_after_the_actual_render_finishes(self):
        from backend.scraper.acquisition import fetch
        pending = MAIN_HTML.replace('<div class="c-right-detail__content">',
                                    '<div class="c-right-detail__content" aria-busy="true">')
        with patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch',
                   return_value=FetchResult(MAIN_URL, status=200, html=MAIN_HTML, transport='browser')) as browser:
            result = fetch(FetchRequest(MAIN_URL, purpose='article', policy={'expected_article_title': MAIN_TITLE}),
                http_transport=lambda _: FetchResult(MAIN_URL, status=200, html=pending + '<script>fetch("/api/article")</script>'))
        self.assertTrue(result.ok)
        browser.assert_called_once()

    def test_unrelated_busy_widget_cannot_block_ready_article_material(self):
        for contract in ('title_bound', 'article_region', 'explicit_selector'):
            with self.subTest(contract=contract):
                soup = BeautifulSoup(MAIN_HTML, 'lxml')
                region = soup.select_one('.c-right-detail__content')
                region['id'] = 'body'
                if contract == 'article_region':
                    region.name = 'article'
                region.append(BeautifulSoup('<aside aria-busy="true">推荐内容加载中</aside>', 'lxml').aside)
                result = classify_result(FetchRequest(MAIN_URL, purpose='article',
                    readiness_selector='#body' if contract == 'explicit_selector' else '',
                    policy={'expected_article_title': MAIN_TITLE}), FetchResult(MAIN_URL, status=200, html=str(soup)))
                self.assertTrue(result.ok)

    def test_short_ready_prose_with_nested_metadata_ignores_an_unrelated_busy_widget(self):
        html = (f'<article><h1>{MAIN_TITLE}</h1><div><p>发布时间：20260703</p>'
            '<p>请同学们按照正式通知公布的时间和相关要求，及时办理本次活动的报名手续。</p>'
            '<aside aria-busy="true">推荐通知加载中</aside></div></article>')
        result = classify_result(FetchRequest(MAIN_URL, purpose='article'),
                                 FetchResult(MAIN_URL, status=200, html=html))
        self.assertTrue(result.ok)


class ExploratoryArticleOnboardingTests(unittest.TestCase):
    def setUp(self):
        from tests import test_direct_onboarding as fixture_module
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_default_onboarding_binds_sample_titles_and_installs_a_custom_static_body(self):
        from tests import test_direct_onboarding as fixture_module
        from backend.database.models import Department, Announcement
        from backend.services.source_onboarding import onboard_page
        reads = []
        def raw(request):
            reads.append(request)
            if request.url == fixture_module.URL:
                html = fixture_module.HTML
            else:
                title = fixture_module.TITLES[int(request.url.rsplit('/', 1)[-1].split('.')[0])]
                html = HTML.replace(TITLE, title)
            return FetchResult(request.url, status=200, html=html)
        with patch('backend.scraper.acquisition.coordinator.http_fetch', side_effect=raw), \
             patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch',
                   side_effect=AssertionError('All bodies are present in static material')), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No paid recognition needed')):
            result = onboard_page({'school_id': self.fixture.school_id, 'url': fixture_module.URL,
                'ai_assist': False, 'observed_columns': [fixture_module.ai_column(content_selector='')]})
        self.assertEqual(result['state'], 'connected', result)
        self.assertEqual(Department.query.one().content_selector, '')
        self.assertEqual(Announcement.query.count(), 3)
        article_reads = [request for request in reads if request.purpose == 'article']
        self.assertEqual(len(article_reads), 3)
        self.assertEqual([request.policy['expected_article_title'] for request in article_reads], fixture_module.TITLES)

    def test_short_semantic_body_keeps_real_prose_beside_its_publication_line(self):
        from backend.services.source_validation import article_samples
        title = '关于开展学生课程调整工作的通知'
        body = '请各学院按照学校公布的课程调整工作安排组织相关教学活动，及时通知选课学生并按规定办理课程变更。'
        html = f'<article><h1>{title}</h1><p>发布时间：2026-10-01</p><p>{body}</p></article>'
        samples = article_samples({'list_url': 'https://www.cupk.edu.cn/library/', 'content_selector': 'article'},
            [{'url': URL, 'title': title}], lambda *_: html)
        self.assertTrue(samples['passed'])
        self.assertIn(body, samples['samples'][0]['body_excerpt'])

    def test_sample_validation_cannot_count_hidden_or_pending_body_prose(self):
        from backend.services.source_validation import article_samples
        for attribute in ('hidden', 'aria-busy="true"'):
            with self.subTest(attribute=attribute):
                html = MAIN_HTML.replace('<div class="c-right-detail__content">',
                                        f'<div class="c-right-detail__content" {attribute}>')
                result = article_samples({'list_url': 'https://www.cupk.edu.cn/tzgg/', 'content_selector': ''},
                    [{'title': MAIN_TITLE, 'url': MAIN_URL}], lambda *_: html)
                self.assertFalse(result['passed'])


if __name__ == '__main__':
    unittest.main()
