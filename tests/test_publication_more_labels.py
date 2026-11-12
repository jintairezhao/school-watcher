"""Observed library actions must preserve their own publishing-region entrances."""
import unittest

from bs4 import BeautifulSoup

from backend.scraper.discovery.publication_lists import is_more, publication_lists
from backend.scraper.discovery.structure import extract_structure


ROOT = 'https://example.edu.cn/library/'


def widget(name, action, target, start):
    items = ''.join(f'<li><a href="/library/c/2026-09-28/{start+i}.shtml">'
                    f'图书馆服务安排通知第{i}项</a><span>2026-09-28</span></li>' for i in range(3))
    return (f'<section><div class="gonggao-title">{name}</div><div class="detail"><ul>{items}</ul>'
            f'<div class="gonggao-more"><a href="{target}">{action}</a></div></div></section>')


class PublicationMoreLabelTests(unittest.TestCase):
    def test_messages_and_resources_keep_separate_observed_entrances(self):
        html = (widget('最新消息', '更多消息', '/library/gndt/', 10000)
                + widget('最新资源', '更多资源', '/library/zygg/', 20000))
        feeds = publication_lists(html, ROOT)
        self.assertEqual({f['name']: f['column_url'] for f in feeds},
                         {'最新消息': ROOT+'gndt/', '最新资源': ROOT+'zygg/'})
        soup = BeautifulSoup(html, 'lxml')
        for feed in feeds:
            link = soup.select_one(feed['column_link_locator'])
            self.assertIsNotNone(link)
            self.assertEqual(link['href'], '/library/'+('gndt/' if feed['name']=='最新消息' else 'zygg/'))
        entries = [link for link in extract_structure(html, ROOT, 'https://example.edu.cn/')['links']
                   if link['url'] in (ROOT+'gndt/', ROOT+'zygg/')]
        self.assertEqual({(link['label'], link['kind'], link['decision']) for link in entries},
                         {('最新消息', 'channel', 'follow'), ('最新资源', 'channel', 'follow')})

    def test_conflicting_destinations_stay_unresolved(self):
        html = widget('最新消息', '更多消息', '/library/gndt/', 10000).replace(
            '</section>', '<a href="/library/other/">更多资源</a></section>')
        self.assertEqual(publication_lists(html, ROOT)[0]['column_url'], '')

    def test_action_words_do_not_accept_articles_or_arbitrary_more_prefixes(self):
        for text in ('更多消息', '更多资源', '更多消息 >'):
            self.assertTrue(is_more(text), text)
        for text in ('更多消息请联系图书馆', '更多资源的使用方法', '消息发布安排'):
            self.assertFalse(is_more(text), text)
        html = widget('最新消息', '更多消息', '/library/c/2026-09-28/538835.shtml', 10000)
        self.assertEqual(publication_lists(html, ROOT)[0]['column_url'], '')


if __name__ == '__main__':
    unittest.main()
