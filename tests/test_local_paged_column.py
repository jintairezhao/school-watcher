import unittest
from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.engine import _configured_entry_region,_next_page_url

URL='https://example.edu.cn/archive?first-token'


def page(number=1,name='通知公告'):
    rows=''.join(f'<li><a href="/info/2026-11-17/{i}.htm">关于第{i}项申报工作的通知</a><span>2026-11-17</span></li>' for i in range(1,5))
    return f'<main><div class="archive-title">{name}</div><section><ul>{rows}</ul>' \
        f'<div class="page-num"><a class="checked" href="#">{number}</a>' \
        '<a href="?next-token">2</a><a href="?next-token">&gt;&gt;</a></div></section></main>'


class LocalPagedColumnTests(unittest.TestCase):
    def config(self):
        feed=publication_lists(page(),URL)[0]
        return dict(name='原部门显示名称',group_name='组织机构',list_url=URL,
            list_selector=feed['list_selector'],title_selector='a',link_selector='a',date_selector='span',content_selector='article')

    def test_pager_arrow_is_not_a_more_column_link(self):
        feed=publication_lists(page(),URL)[0]
        self.assertEqual(feed['name'],'通知公告')
        self.assertFalse(feed['column_url'])

    def test_local_first_page_control_binds_a_complete_named_archive(self):
        config=self.config()
        binding=_configured_entry_region(config,page(),URL)
        self.assertIsNotNone(binding)
        self.assertEqual(binding['binding'],'configured_paged_heading')
        self.assertIsNotNone(_configured_entry_region(config,page(2),URL+'-page2',binding=binding))
        self.assertIsNone(_configured_entry_region(config,page(2),URL))
        self.assertIsNone(_configured_entry_region(config,page(2,'学院新闻'),URL+'-page2',binding=binding))
        self.assertIsNone(_configured_entry_region(config,page().replace('class="checked"',''),URL))
        self.assertIsNone(_configured_entry_region(dict(config,list_selector='li:nth-of-type(1)'),page(),URL))
        scope=dict(container_selector=binding['container'],heading_selector=binding['heading'])
        self.assertEqual(_next_page_url(page(),URL,1,list_selector=config['list_selector'],scope=scope),
            'https://example.edu.cn/archive?next-token')


if __name__=='__main__':unittest.main()
