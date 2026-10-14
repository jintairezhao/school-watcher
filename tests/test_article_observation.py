"""Custom article layouts must reach both body and publication metadata readers."""
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department
from backend.scraper.acquisition import FetchRequest, FetchResult

TITLE = '关于2025-2026学年克校区国家奖学金候选人名单公示'
URL = 'https://www.cupk.edu.cn/xszz/c/2026-10-13/539089.shtml'
HTML = (Path(__file__).parent / 'fixtures' / 'article-custom-layout.html').read_text(encoding='utf-8')


class ArticleObservationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()
        self.source = Department(school_id=self.fixture.school_id, name='学生资助',
            list_url='https://www.cupk.edu.cn/xszz/', list_selector='li', title_selector='a',
            link_selector='a', date_selector='span.date', content_selector='')
        db.session.add(self.source); db.session.flush()
        self.ann = Announcement(school_id=self.source.school_id, department_id=self.source.id,
            title=TITLE, url=URL)
        db.session.add(self.ann); db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def test_deep_title_wrappers_do_not_hide_explicit_publication_metadata(self):
        from backend.services.publication_dates import inspect_article_date
        proof = inspect_article_date(HTML, URL, TITLE)
        self.assertEqual(proof['state'], 'verified')
        self.assertEqual(proof['date'], '2026-10-13')
        self.assertTrue(proof['locator'])

    def test_month_day_list_queues_one_article_read_without_guessing_a_year(self):
        from backend.scraper.engine import _process_announcement_item
        item = BeautifulSoup(f'<li><a href="{URL}">{TITLE}</a><span class="date">10-13</span></li>', 'lxml').li
        for _ in range(3):
            _process_announcement_item(item, self.source, self.source.school.url)
            db.session.commit()
        self.assertIsNone(self.ann.published_at)
        self.assertEqual(BackgroundTask.query.filter_by(kind='publication_date').count(), 1)

    def test_real_acquisition_and_body_reader_accept_title_bound_custom_layout(self):
        from backend.services.content_cache import fetch_content
        raw = FetchResult(URL, status=200, html=HTML)
        with patch('backend.scraper.acquisition.coordinator.http_fetch', return_value=raw) as fetch, \
                patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch') as browser:
            result = fetch_content(self.ann.id)
        self.assertTrue(result['saved'])
        self.assertEqual(self.ann.published_at, datetime(2026, 10, 13))
        self.assertIn('拟推荐以下181名学生', self.ann.content_text)
        self.assertNotIn('站内搜索', self.ann.content_text)
        self.assertNotIn('相关公告', self.ann.content_text)
        self.assertIsNotNone(BeautifulSoup(self.ann.content_html, 'lxml').table)
        fetch.assert_called_once(); browser.assert_not_called()

    def test_different_article_title_cannot_unlock_a_custom_body(self):
        from backend.scraper.acquisition import classify_result
        request = FetchRequest(URL, purpose='article', policy={'expected_article_title':'完全不同的通知'})
        result = classify_result(request, FetchResult(URL, status=200, html=HTML, transport='browser'))
        self.assertFalse(result.ok)

    def test_unrelated_common_body_region_cannot_replace_the_identified_notice(self):
        from backend.services.content_cache import fetch_content
        soup=BeautifulSoup(HTML,'lxml')
        other=soup.new_tag('div',attrs={'class':'TRS_Editor'});other.string='另一条推荐公告的正文，不能混进当前通知。'
        soup.aside.append(other)
        with patch('backend.scraper.acquisition.coordinator.http_fetch',return_value=FetchResult(URL,status=200,html=str(soup))):
            fetch_content(self.ann.id)
        self.assertIn('拟推荐以下181名学生',self.ann.content_text)
        self.assertNotIn('另一条推荐公告',self.ann.content_text)

    def test_metadata_is_saved_even_when_a_body_rule_cannot_be_satisfied(self):
        from backend.services.content_cache import fetch_content
        soup = BeautifulSoup(HTML, 'lxml')
        soup.select_one('.c-right-detail__content').decompose()
        with patch('backend.scraper.acquisition.coordinator.http_fetch', return_value=FetchResult(URL,status=200,html=str(soup))):
            with self.assertRaises(Exception):
                fetch_content(self.ann.id)
        self.assertEqual(self.ann.published_at, datetime(2026,10,13))
        self.assertFalse(self.ann.content_cached_at)

    def test_css_renaming_and_more_title_wrappers_do_not_change_results(self):
        from backend.services.publication_dates import inspect_article_date
        from backend.scraper.article_observation import title_bound_body
        soup = BeautifulSoup(HTML,'lxml')
        for node in soup.select('[class]'):
            node['class'] = ['arbitrary-layout']
        node = next(n for n in soup.select('div') if n.get_text(strip=True)==TITLE)
        for _ in range(4):
            wrapper = soup.new_tag('section'); node.wrap(wrapper); node = wrapper
        self.assertEqual(inspect_article_date(str(soup),URL,TITLE)['date'],'2026-10-13')
        self.assertIn('拟推荐以下181名学生',title_bound_body(soup,URL,TITLE).get_text())

    def test_static_cms_form_is_an_article_wrapper_not_a_login_form(self):
        from backend.services.publication_dates import inspect_article_date
        html=f'<form name="_newscontent_fromname"><input type="hidden" value="123"><h1>{TITLE}</h1><div>发布时间：2026-10-13</div><div id="vsb_content"><p>这是正文材料，不能因为外层静态表单而丢失日期。</p></div></form>'
        self.assertEqual(inspect_article_date(html,URL,TITLE)['date'],'2026-10-13')

    def test_body_prose_and_neighbor_widget_cannot_supply_a_missing_publication_date(self):
        from backend.services.publication_dates import inspect_article_date
        soup = BeautifulSoup(HTML,'lxml')
        soup.select_one('.SecondList__msg').decompose()
        p = soup.new_tag('p'); p.string = '发布日期：2026-10-14'
        soup.select_one('.c-right-detail__content').append(p)
        self.assertEqual(inspect_article_date(str(soup),URL,TITLE)['state'],'unknown')

    def test_header_only_http_document_still_renders_the_missing_body(self):
        from backend.services.content_cache import fetch_content
        soup=BeautifulSoup(HTML,'lxml')
        soup.select_one('.c-right-detail__content').clear()
        soup.body.append(soup.new_tag('script',src='/actual-article-loader.js'))
        with patch('backend.scraper.acquisition.coordinator.http_fetch',return_value=FetchResult(URL,status=200,html=str(soup))), \
                patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch',return_value=FetchResult(URL,status=200,html=HTML,transport='browser')) as browser:
            self.assertTrue(fetch_content(self.ann.id)['saved'])
        browser.assert_called_once()
        self.assertEqual(self.ann.published_at,datetime(2026,10,13))

    def test_old_unknown_result_reopens_once_and_then_reuses_verified_evidence(self):
        from backend.services.publication_dates import resume_date_checks,check_date,apply_listing_date
        from backend.services import tasks
        job = tasks.enqueue('publication_date',self.ann.id,{'announcement_id':self.ann.id})
        job.state='done'; job.result={'state':'unknown','checked_full':True}; db.session.commit()
        resume_date_checks(); db.session.refresh(job)
        self.assertEqual(job.state,'pending')
        with patch('backend.scraper.acquisition.coordinator.http_fetch',return_value=FetchResult(URL,status=200,html=HTML)) as fetch:
            self.assertEqual(check_date(self.ann.id)['date'],'2026-10-13')
            job.state='done'; db.session.commit()
            for _ in range(3):
                apply_listing_date(self.ann,self.source,{'role':'ambiguous','raw':'10-13'},None)
                resume_date_checks()
            fetch.assert_called_once()
        self.assertEqual(job.state,'done')


if __name__ == '__main__':
    unittest.main()
