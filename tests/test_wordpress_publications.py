"""Article permalinks and taxonomy links must not exchange their roles."""
import unittest

from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.publication_lists import publication_lists

ROOT = 'https://teach.example.edu.cn/'
COLUMN = ROOT + 'category/notice/notice-exam'
ARTICLE = ROOT + 'notice/notice-exam/20548.html'
TITLE = '关于2026年大学英语考试报名有关工作的通知'


def rows():
    return ''.join(f'<li class="post-{i} post type-post"><span class="cat-tags">'
        f'<a href="{COLUMN}">考试</a></span><span class="post">'
        f'<a href="{ROOT}notice/notice-exam/{i}.html">{TITLE}{i}</a></span>'
        '<span class="date">2026-09-10 09:31</span></li>' for i in (20548, 20549))


def article_html(column=COLUMN):
    return (f'<body class="single single-post postid-20548"><div class="breadcrumbs">'
        f'<a class="taxonomy category" href="{column}">考试</a></div>'
        f'<h1><a href="{ARTICLE}">{TITLE}</a></h1><main><article id="post-20548" class="type-post">'
        '<p>请符合条件的同学按照学校要求在指定时间提交报名材料。</p></article></main>'
        f'<ul><li class="meta-categories"><a rel="tag" href="{column}">考试</a></li></ul></body>')


class WordPressPublicationTests(unittest.TestCase):
    def test_search_results_follow_categories_not_five_digit_article_titles(self):
        html = '<body class="search"><main><ul class="article-list with-tag">' + rows() + '</ul></main></body>'
        result = extract_structure(html, ROOT + 'search/考试', ROOT, 'channel', '考试', [])
        self.assertEqual({link['url'] for link in result['links'] if link['decision'] == 'follow'}, {COLUMN})
        self.assertEqual(len([link for link in result['links'] if link['decision'] == 'article_reference']), 2)
        self.assertFalse(publication_lists(html, ROOT + 'search/考试'))

    def test_archive_uses_official_crumb_and_post_title_not_category_tag(self):
        html = ('<body class="archive category"><div class="breadcrumbs"><a href="/">首页</a>'
            '<span>考试</span></div><h1>考试</h1><main><ul class="article-list with-tag">' + rows() + '</ul></main></body>')
        feeds = publication_lists(html, COLUMN)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '考试')
        self.assertEqual(feeds[0]['column_url'], COLUMN)
        self.assertEqual(feeds[0]['item_count'], 2)
        self.assertTrue(all(s['title'].startswith(TITLE) for s in feeds[0]['samples']))

    def test_single_article_does_not_become_source_from_related_articles(self):
        html = article_html().replace('</body>', '<aside><h2>相关通知</h2><ul>' + rows() + '</ul></aside></body>')
        self.assertFalse(publication_lists(html, ARTICLE))

    def test_plain_list_with_numeric_urls_is_still_a_column(self):
        html = '<section><h2>通知公告</h2><ul class="article-list">' + rows() + '</ul></section>'
        self.assertTrue(publication_lists(html, ROOT + '12345.html'))


if __name__ == '__main__':
    unittest.main()
