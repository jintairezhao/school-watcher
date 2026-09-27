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


if __name__ == '__main__':
    unittest.main()
