import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.article_provenance import article_provenance

URL = 'https://www.cupk.edu.cn/syxy/c/2026-06-10/534685.shtml'


def article(metadata, body='<p>正文提到来源：其他研究机构，不是文章的来源字段。</p>'):
    return ('<div class="c-right-detail"><div class="c-right-detail__header">研究成果</div>'
            '<div class="SecondList__msg">' + metadata + '</div><div class="c-right-detail__content">' + body + '</div></div>')


class ArticleProvenanceTests(unittest.TestCase):
    def test_source_is_literal_and_not_author_current_website_or_body_mentions(self):
        html = article('发布日期：2026-06-10 作者：石油学院管理员 来源：351-石油学院 浏览：767')
        result = article_provenance(html, URL)
        self.assertEqual(result['labels'], ['351-石油学院'])
        self.assertEqual(result['state'], 'observed')
        self.assertEqual(result['evidence'][0]['reference_url'], URL)
        self.assertEqual(result['evidence'][0]['content_hash'], hashlib.sha256(html.encode()).hexdigest())
        self.assertNotIn('石油学院管理员', result['labels'])

    def test_empty_source_field_author_only_and_body_metadata_do_not_invent_signature(self):
        for metadata in ('发布日期：2026-06-10 作者：教务处 浏览：2', '来源： 浏览：2', '来源：'):
            result = article_provenance(article(metadata), URL)
            self.assertEqual(result['labels'], [])
            self.assertEqual(result['state'], 'field_unavailable')
        quoted = article('来源：被引用机构 浏览：1')
        result = article_provenance(article('作者：教务处', quoted), URL)
        self.assertEqual(result['labels'], [])

    def test_repeated_and_different_signatures_preserve_all_evidence(self):
        one = article('来源：地球科学与工程学院 浏览：1')
        repeated = article_provenance(one + one, URL)
        self.assertEqual(repeated['labels'], ['地球科学与工程学院'])
        self.assertEqual(len(repeated['evidence']), 2)
        different = article_provenance(one + article('稿件来源：新华社 编辑：编辑甲'), URL)
        self.assertEqual(different['labels'], ['地球科学与工程学院', '新华社'])
        self.assertEqual(different['state'], 'multiple_signatures')

    def test_reparse_uses_current_saved_text_and_other_hosts_need_their_own_adapter(self):
        self.assertEqual(article_provenance(article('来源：甲学院'), URL)['labels'], ['甲学院'])
        self.assertEqual(article_provenance(article('来源：乙学院'), URL)['labels'], ['乙学院'])
        self.assertEqual(article_provenance(article('来源：甲学院'), 'https://www.example.edu.cn/a')['state'], 'unrecognized')
        self.assertEqual(article_provenance('', URL)['state'], 'no_saved_html')

    def test_source_text_encoded_as_html_remains_text(self):
        result = article_provenance(article('来源：&lt;img src=x onerror=alert(1)&gt; 浏览：2'), URL)
        self.assertEqual(result['labels'], ['<img src=x onerror=alert(1)>'])

    def test_older_campus_metadata_locations_require_the_observed_title_shell(self):
        html = article('作者：管理员').replace('<div class="SecondList__msg">作者：管理员</div>',
            '<div class="c-right-detail__sub-title"><div class="c-main__time">来源：353-工学院 浏览：10</div></div>')
        self.assertEqual(article_provenance(html, URL)['labels'], ['353-工学院'])
        old = ('<div class="c-main__right"><div class="c-right-detail"><div class="c-right-detail__header">竞赛目录</div></div>'
               '<div class="c-right-detail__content"><div class="c-main__time SecondList__msg">来源：05-创新创业学院 浏览：10</div></div>'
               '<table><tr><td>竞赛条目</td></tr></table></div>')
        self.assertEqual(article_provenance(old, URL)['labels'], ['05-创新创业学院'])
        self.assertEqual(article_provenance(old.replace('c-right-detail__header', 'body-heading'), URL)['labels'], [])

    def test_main_campus_news_and_empty_department_fields_do_not_use_funding_sources(self):
        url = 'https://www.cup.edu.cn/news/sx/example.htm'
        html = ('<div class="subPageArticle"><div class="articleTitle">校园新闻</div><div class="articleAuthor"><p>'
                '<span>发布时间:2026-07-06</span> | <span>来源：外国语学院</span> | <span>作者：通讯员</span> | <span>浏览量：</span>'
                '</p></div><div class="article"><p>经费来源：科研项目。</p></div></div>')
        self.assertEqual(article_provenance(html, url)['labels'], ['外国语学院'])
        empty = '<div class="subArticleTitle"><h2>部门公告</h2><span>发布时间：2026-09-20</span><span>来源：</span></div>'
        self.assertEqual(article_provenance(empty + '<p>经费来源：科研项目。</p>', url)['labels'], [])
        self.assertEqual(article_provenance('<p>经费来源：科研项目。</p>', url)['labels'], [])


if __name__ == '__main__':
    unittest.main()
