import unittest
import json

from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.discovery.structure import extract_structure

URL = 'https://dangzheng.nwpu.edu.cn/'
SCRIPT = '''$(".sect1-r .title h2 span").mouseenter(function() {
    $(this).addClass("on").siblings().removeClass("on");
    var i = $(this).index();
    $(".sect1-r .s1-qh ul").eq(i).addClass("on").siblings().removeClass("on");
    $(".sect1-r .title .more a").eq(i).addClass("on").siblings().removeClass("on");
})'''


def items(start):
    return ''.join(f'<li><a href="/info/1/{start+i}.htm">关于组织本学期学术交流的通知{i}</a><span>2026-09-16</span></li>' for i in range(3))


def tabs(script=SCRIPT, extra=''):
    return ('<div class="sect1-r"><div class="title"><h2><span>时政<i>要闻</i></span>'
            '<span>部门<i>动态</i></span>' + extra + '</h2><div class="more">'
            '<a href="/politics/">查看更多</a><a href="/office/">查看更多</a></div></div>'
            '<div class="s1-qh"><ul>' + items(10000) + '</ul><ul>' + items(20000) +
            '</ul></div></div><script>' + script + '</script>')


class PublicationHeadingTests(unittest.TestCase):
    def test_indexed_switch_requires_exact_official_control_relationship(self):
        feeds = publication_lists(tabs(), URL)
        self.assertEqual({f['name']: f['column_url'] for f in feeds},
                         {'时政要闻': URL + 'politics/', '部门动态': URL + 'office/'})
        for feed in feeds:
            expected = 10000 if feed['name'] == '时政要闻' else 20000
            self.assertEqual({s['url'] for s in feed['samples']}, {URL + f'info/1/{expected+i}.htm' for i in range(3)})
        more = [l for l in extract_structure(tabs(), URL, 'https://www.nwpu.edu.cn/')['links'] if l.get('raw_label') == '查看更多']
        self.assertEqual({(l['label'], l['url']) for l in more}, {('时政要闻', URL + 'politics/'), ('部门动态', URL + 'office/')})

    def test_changed_commented_or_missing_switch_is_not_guessed(self):
        for script in ('', '/*' + SCRIPT + '*/', SCRIPT.replace('.eq(i)', '.eq(1-i)'),
                       'const example = ' + json.dumps(SCRIPT) + ';',
                       SCRIPT + ';' + SCRIPT.replace('.eq(i)', '.eq(1-i)')):
            feeds = publication_lists(tabs(script), URL)
            self.assertTrue(feeds)
            self.assertTrue(all(not f['name'] and f.get('heading_ambiguous') for f in feeds))
        feeds = publication_lists(tabs(extra='<b>额外索引</b>'), URL)
        self.assertTrue(all(not f['name'] for f in feeds))

    def test_nested_layout_wrappers_keep_one_explicit_heading(self):
        html = '<div><div class="tit"><h1>文章推荐</h1><a href="/recommended/">查看更多</a></div>'
        html += '<div><div><div><ul>' + items(10000) + '</ul></div><script>noop()</script></div><div style="clear:both"></div></div></div>'
        feed = publication_lists(html, URL)[0]
        self.assertEqual((feed['name'], feed['column_url']), ('文章推荐', URL + 'recommended/'))

    def test_unnamed_list_cannot_borrow_a_neighbor_or_overarching_title(self):
        html = '<div><h1>学校新闻</h1><section><h2>其他学院</h2><ul>' + items(20000) + '</ul></section>'
        html += '<div><div><div><ul>' + items(10000) + '</ul></div></div></div></div>'
        feeds = publication_lists(html, URL)
        anonymous = next(f for f in feeds if any('/10000.' in s['url'] for s in f['samples']))
        self.assertEqual(anonymous['name'], '')

    def test_unnamed_candidate_does_not_inherit_the_college_name(self):
        from backend.services.source_catalog import publication_candidates
        evidence = {'lists': publication_lists('<section><ul>' + items(10000) + '</ul></section>', URL)}
        report = {'site': {'root_url': URL}, 'pages': [{'feed_json': json.dumps(evidence),
                  'state': 'fetched', 'path_json': '[]', 'kind': 'unit', 'label': '工程学院',
                  'url': URL, 'final_url': URL, 'health': 'date_unknown'}]}
        candidates = publication_candidates(report)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['name'], '栏目名称待核实')
        self.assertTrue(candidates[0]['source_heading_pending'])
        self.assertEqual(candidates[0]['source_page_label'], '工程学院')

    def test_article_card_heading_is_detected_from_observed_membership(self):
        # Real layout shape: sibling article cards carry a title CSS class.
        # Opaque article addresses need no CMS-specific URL convention.
        html = ('<div class="info-box3"><div class="info-text1">- 讲座信息</div>'
                '<div class="clear"></div>'
                '<a class="info-text3 yellow_title" href="talk/alpha.htm">2026/09/16<br>'
                '关于人工智能前沿发展的学术讲座通知</a>'
                '<a class="info-text3 yellow_title" href="talk/beta.htm">2026/09/15<br>'
                '关于计算机科学研究方法的学术讲座通知</a>'
                '<div class="info-img2"><a href="talk/index.htm"><img src="arrow.png"></a></div></div>')
        feeds = publication_lists(html, URL)
        self.assertTrue(feeds)
        for feed in feeds:
            self.assertEqual(feed['name'], '')
            self.assertEqual(feed['column_url'], '')
            self.assertTrue(feed['heading_article_reference'])
            self.assertEqual(len(feed['article_urls']), 2)

    def test_article_title_cannot_make_the_whole_region_disappear(self):
        html = ('<section><h2>关于举办本学期学术交流活动的通知</h2><ul>'
                '<li><a href="opaque-a.htm">关于举办本学期学术交流活动的通知</a><time>2026-09-16</time></li>'
                '<li><a href="opaque-b.htm">关于召开研究生培养工作交流会的通知</a><time>2026-09-15</time></li>'
                '</ul></section>')
        feeds = publication_lists(html, URL)
        self.assertTrue(feeds)
        self.assertTrue(all(not feed['name'] and feed['heading_article_reference'] for feed in feeds))

    def test_opaque_real_column_address_is_not_rejected_by_filename(self):
        html = '<section><h2><a href="opaque-list.htm">通知公告</a></h2><ul>' + items(10000) + '</ul></section>'
        feeds = publication_lists(html, URL)
        self.assertTrue(feeds)
        self.assertEqual((feeds[0]['name'], feeds[0]['column_url']), ('通知公告', URL + 'opaque-list.htm'))

    def test_shallow_list_does_not_emit_a_document_root_css_selector(self):
        html = ('<div><h2>通知公告</h2>'
                '<a href="opaque-a.htm">2026/09/16<br>关于本学期教学安排的重要通知</a>'
                '<a href="opaque-b.htm">2026/09/15<br>关于研究生培养事项的重要通知</a></div>')
        feeds = publication_lists(html, URL)
        self.assertTrue(feeds)
        self.assertEqual(feeds[0]['name'], '通知公告')
        self.assertNotIn('[document]', feeds[0]['list_selector'])

    def test_anonymous_neighbour_keeps_its_own_repeated_article_occurrences(self):
        html = ('<section><h2>通知公告</h2><ul>' + items(10000) + '</ul></section>'
                '<section><ul>' + items(10000) + '</ul></section>')
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 2)
        self.assertEqual({feed['name'] for feed in feeds}, {'通知公告', ''})
        positions = [{link['locator'] for link in feed['article_links']} for feed in feeds]
        self.assertTrue(positions[0].isdisjoint(positions[1]))

    def test_shared_module_heading_is_group_context_not_each_column_name(self):
        html = ('<section><h2>文化日历</h2><div><ul>' + items(10000) +
                '</ul></div><div><ul>' + items(20000) + '</ul></div></section>')
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 2)
        self.assertTrue(all(not feed['name'] for feed in feeds))
        self.assertTrue(all(feed['column_group_name'] == '文化日历' for feed in feeds))
        self.assertTrue(all(feed['heading_ambiguous'] for feed in feeds))


if __name__ == '__main__':
    unittest.main()
