"""Keep observed homepage archive entrances across reader and AI-review passes."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.discovery.structure import extract_structure
from tests import test_navigation_execution_events as navigation_fixture

ROOT = navigation_fixture.ROOT


def news_window(number=21, name='通知公告', target='/notice/list.htm'):
    rows = ''.join(f'<li class="news"><a title="关于开展第{i}项学生事务工作的通知" '
                   f'href="/2026/1101/c100a{12000+i}/page.htm">关于开展第{i}项学生事务工作的通知</a>'
                   '<span class="news_meta">2026-11-01</span></li>' for i in range(3))
    return (f'<div class="post mbox" frag="窗口{number}" portletmode="simpleNews">'
            f'<div class="tt"><h3 class="tit"><span frag="标题">{name}</span></h3>'
            f'<div class="more_btn" frag="按钮" type="更多"><a href="{target}">'
            '<span><img src="/images/action.png" alt=""/></span></a></div></div>'
            f'<div class="con"><ul class="news_list">{rows}</ul></div></div>')


class HomepagePublicationEntranceTests(unittest.TestCase):
    def test_empty_window_keeps_its_literal_archive_without_claiming_publications(self):
        soup = BeautifulSoup(news_window(name='新闻动态', target='/news/list.htm'), 'lxml')
        soup.select_one('.con').clear()
        html = str(soup)
        self.assertFalse(publication_lists(html, ROOT))
        parsed = extract_structure(html, ROOT, ROOT, 'channel', '研究生培养')
        self.assertEqual([(r['label'],r['url']) for r in parsed['links']], [('新闻动态', ROOT+'news/list.htm')])
        self.assertEqual([n['relation'] for n in parsed['nodes'] if n['url']==ROOT+'news/list.htm'], ['navigation_entry'])

    def test_empty_window_does_not_borrow_hidden_conflicting_or_foreign_archive_actions(self):
        for change in ('hidden','duplicate_title','duplicate_action','foreign','wrong_action'):
            with self.subTest(change=change):
                soup = BeautifulSoup(news_window(name='新闻动态',target='/news/list.htm'), 'lxml')
                soup.select_one('.con').clear()
                title, action = soup.select_one('[frag="标题"]'), soup.select_one('[frag="按钮"]')
                if change == 'hidden': action['hidden'] = ''
                elif change == 'duplicate_title': title.parent.append(deepcopy(title))
                elif change == 'duplicate_action': action.parent.append(deepcopy(action))
                elif change == 'foreign': action.a['href'] = 'https://outside.example.net/news/list.htm'
                else: action['type'] = '下载'
                parsed = extract_structure(str(soup), ROOT, ROOT, 'channel','研究生培养')
                self.assertFalse([r for r in parsed['links'] if r['url'].endswith('/news/list.htm')])

    def test_literal_window_title_and_image_more_action_identify_only_their_own_preview(self):
        feeds = publication_lists(news_window(), ROOT)
        self.assertEqual([(f['name'], f['column_url']) for f in feeds], [('通知公告', ROOT + 'notice/list.htm')])
        self.assertTrue(feeds[0]['module_preview'])
        self.assertFalse(feeds[0]['full_column_equivalent'])

    def test_other_simple_news_templates_keep_their_existing_heading_path(self):
        rows = ''.join(f'<li><a title="关于第{i}项学生事务安排的通知" href="/2026/1101/c100a{14000+i}/page.htm">'
                       f'关于第{i}项学生事务安排的通知</a><span>2026-11-01</span></li>' for i in range(3))
        html = ('<div frag="窗口9" portletmode="simpleNews"><section>'
                '<h2>通知公告 <a href="/notice/list.htm">更多</a></h2>'
                f'<ul>{rows}</ul></section></div>')
        self.assertEqual([(f['name'], f['column_url']) for f in publication_lists(html, ROOT)],
                         [('通知公告', ROOT + 'notice/list.htm')])

    def test_hidden_conflicting_foreign_or_neighboring_actions_do_not_name_a_preview(self):
        for change in ('hidden', 'duplicate_title', 'duplicate_action', 'foreign', 'wrong_action'):
            with self.subTest(change=change):
                soup = BeautifulSoup(news_window(), 'lxml')
                title, action = soup.select_one('[frag="标题"]'), soup.select_one('[frag="按钮"]')
                if change == 'hidden':
                    action['hidden'] = ''
                elif change == 'duplicate_title':
                    title.parent.append(deepcopy(title))
                elif change == 'duplicate_action':
                    action.parent.append(deepcopy(action))
                elif change == 'foreign':
                    action.a['href'] = 'https://outside.example.net/list.htm'
                else:
                    action['type'] = '下载'
                self.assertTrue(all(not f['name'] and not f['column_url']
                                    for f in publication_lists(str(soup), ROOT)))

    def test_two_neighboring_windows_keep_separate_titles_and_archives(self):
        feeds = publication_lists(news_window() + news_window(22, '学位办理', '/degree/list.htm'), ROOT)
        self.assertEqual({f['name']: f['column_url'] for f in feeds},
                         {'通知公告': ROOT + 'notice/list.htm', '学位办理': ROOT + 'degree/list.htm'})

    def test_navigation_review_preserves_the_archive_edge_saved_by_the_reader(self):
        html = ('<title>测试大学</title><nav><a href="/space/">成长空间</a></nav>'
                '<section><h2>通知公告 <a href="/notice/list.htm">更多&gt;&gt;</a></h2><ul>'
                + ''.join(f'<li><a title="关于做好第{i}项学生事务的通知" href="/2026/1101/c100a{13000+i}/page.htm">'
                          f'关于做好第{i}项学生事务的通知</a><span>2026-11-01</span></li>' for i in range(3))
                + '</ul></section>')
        fixture = navigation_fixture.NavigationExecutionEventTests()
        with patch.object(navigation_fixture, 'HTML', html):
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        target = ROOT + 'notice/list.htm'

        def edge():
            with fixture.store.connect() as connection:
                return connection.execute('select decision,content_hash,observed_at from edges '
                    'where site_key=? and parent_url=? and target_url=?', (fixture.key, ROOT, target)).fetchone()

        before = dict(edge())
        fixture.run_application(answer=lambda *args, **kwargs: dict(status='processed', reviewed_routes=0, cached_routes=0))
        after = edge()
        self.assertIsNotNone(after, 'A later navigation review erased the observed archive entrance')
        self.assertEqual(dict(after), before)


if __name__ == '__main__':
    unittest.main()
