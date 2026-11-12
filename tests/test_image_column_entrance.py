"""A real image-only archive action must survive heading extraction."""
import unittest
from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.discovery.structure import extract_structure,add_publication_structure

URL='https://finance.example.edu.cn/'

def module(actions):
    return ('<section><div class="all_tit">新闻动态'+actions+'</div><ul>'+''.join(
        f'<li><a href="/info/1/{10000+i}.htm">关于财务服务办理的通知{i}</a>'
        '<span>2026-10-30</span></li>' for i in range(3))+'</ul></section>')

class ImageColumnEntranceTests(unittest.TestCase):
    def test_image_more_action_preserves_actual_target_and_locator(self):
        html=module('<a href="/news/"><span class="more"><img alt="" src="/more.png"></span></a>')
        feed=publication_lists(html,URL)[0]
        self.assertEqual((feed['name'],feed['column_url']),('新闻动态',URL+'news/'))
        self.assertTrue(feed['column_link_locator'].endswith('a:nth-of-type(1)'))
        parsed=extract_structure(html,URL,URL)
        add_publication_structure(parsed,dict(lists=[feed]))
        nodes=parsed['nodes']
        self.assertTrue(any(n['name']=='新闻动态'and n['url']==URL+'news/'and n['relation']=='publication_column'for n in nodes))

    def test_plain_image_cannot_supply_an_archive(self):
        feed=publication_lists(module('<a href="/other/"><img src="/image.png" alt=""></a>'),URL)[0]
        self.assertEqual(feed['column_url'],'')

    def test_ambiguous_or_article_image_actions_are_not_adopted(self):
        for actions in ('<a class="more" href="/one/"><img></a><a class="more" href="/two/"><img></a>',
                        '<a class="more" href="/info/1/12345.htm"><img></a>'):
            self.assertEqual(publication_lists(module(actions),URL)[0]['column_url'],'')

    def test_neighboring_image_action_cannot_name_the_first_module(self):
        first=module('')
        second=module('<a class="more" href="/second/"><img></a>').replace('新闻动态','通知公告').replace('/info/1/','/info/2/')
        feeds=publication_lists(first+second,URL)
        self.assertEqual({f['name']:f['column_url']for f in feeds},{'新闻动态':'','通知公告':URL+'second/'})

if __name__=='__main__':unittest.main()
