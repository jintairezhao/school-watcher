"""Date-first editorial headers must reach the normal title-bound body checks."""
from unittest.mock import patch
import unittest
from bs4 import BeautifulSoup

from backend.scraper.acquisition import FetchRequest,FetchResult,classify_result
from backend.scraper.article_observation import title_bound_body
from backend.services.publication_dates import inspect_article_date

TITLE='关于开展师生科研服务申报的通知'
URL='https://fixture.example.edu/notice/item.htm'
PROSE='请符合条件的师生按要求准备申报材料，认真核对申请信息，并在通知规定时间内向所在单位提交。'


def page(line,*,body=True):
    # The observed family places an otherwise unlabelled date before its author
    # and source, in a separate sibling between the title and ordinary prose.
    return (f'<html><head><title>{TITLE}</title></head><body><main><div class="notice">'
            f'<div><h3>{TITLE}</h3><li></li></div><div><span>{line}</span></div>'
            + (f'<div><p>{PROSE}</p></div>' if body else '') + '</div></main></body></html>')


class EditorialPublicationLineTests(unittest.TestCase):
    def result(self,html,title=TITLE):
        return classify_result(FetchRequest(URL,purpose='article',policy={'expected_article_title':title}),
                               FetchResult(URL,status=200,html=html))

    def test_date_first_author_source_header_reaches_body_and_date_readers(self):
        for date in ['2026年10月14日','2026-10-14','2026/10/14']:
            with self.subTest(date=date):
                html=page(f'{date} 作者：甲 审核：乙 来源：学院')
                self.assertTrue(self.result(html).ok)
                body=title_bound_body(BeautifulSoup(html,'lxml'),URL,TITLE)
                self.assertIn(PROSE,body.get_text())
                self.assertNotIn('作者：',body.get_text())
                proof=inspect_article_date(html,URL,TITLE)
                self.assertEqual((proof['state'],proof.get('date')),('verified','2026-10-14'))

    def test_events_prose_conflicts_and_invalid_dates_do_not_unlock_custom_body(self):
        for line in ['活动时间：2026-10-14 作者：甲','2026-10-14 作者：甲 截止时间：2026-10-16',
                     '2026-10-14 作者：甲 更新时间：2026-10-15','2026-10-14 至2026-10-15 来源：学院',
                     '2026-02-30 作者：甲','请于2026-10-14联系作者：甲办理手续。',
                     '2026-10-14 会议时间：10:00—12:00 作者：甲']:
            with self.subTest(line=line):
                self.assertIsNone(title_bound_body(BeautifulSoup(page(line),'lxml'),URL,TITLE))
                self.assertFalse(self.result(page(line)).ok)

    def test_header_cannot_supply_a_body_or_unlock_a_different_title(self):
        html=page('2026-10-14 作者：甲 来源：学院')
        self.assertFalse(self.result(html,'完全不同的通知').ok)
        self.assertFalse(self.result(page('2026-10-14 作者：甲 来源：学院',body=False)).ok)
        hidden=html.replace('<div><span>','<div hidden><span>')
        self.assertFalse(self.result(hidden).ok)

    def test_normal_source_validation_uses_observed_title_without_browser_or_model(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.services.source_onboarding import _read
        from backend.services.source_validation import article_samples
        fixture=DirectOnboardingTests();fixture.setUp()
        try:
            html=page('2026-10-14 作者：甲 来源：学院')
            config={'list_url':'https://fixture.example.edu/notices/','content_selector':''}
            def reader(url,purpose):
                return _read(url,purpose,config=config,exploration=True,article_titles={URL:TITLE})
            with patch('backend.scraper.acquisition.coordinator.http_fetch',return_value=FetchResult(URL,status=200,html=html)) as fetch, \
                    patch('backend.scraper.acquisition.browser_client.BrowserClient.fetch',side_effect=AssertionError('No render needed')), \
                    patch('backend.ai.providers.complete',side_effect=AssertionError('No model')):
                validation=article_samples(config,[{'url':URL,'title':TITLE}],reader)
            self.assertTrue(validation['passed'],validation)
            self.assertEqual(validation['matched_count'],1)
            fetch.assert_called_once()
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
