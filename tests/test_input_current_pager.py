"""A pager's current page may be an input value, outside its text nodes."""
from copy import deepcopy
import unittest

from bs4 import BeautifulSoup
from backend.scraper.engine import _counted_pager, _next_page_url

URL='https://example.edu.cn/notices.htm'
ITEMS='<ul id="items"><li><a href="/info/1.htm">正式通知一</a></li><li><a href="/info/2.htm">正式通知二</a></li></ul>'


def pager():
    # Reduced from the retained NJAU master's/doctoral notice controls.
    return BeautifulSoup('''<div class="pb_sys_common pb_sys_normal pb_sys_style1">
      <span class="p_pages"><span class="p_first_d p_fun_d">首页</span>
       <span class="p_prev_d p_fun_d">上页</span><span class="p_no_d">1</span>
       <span class="p_no"><a href="notices/2.htm">2</a></span>
       <span class="p_no"><a href="notices/1.htm">3</a></span>
       <span class="p_next p_fun"><a href="notices/2.htm">下页</a></span>
       <span class="p_last p_fun"><a href="notices/1.htm">尾页</a></span></span>
      <span class="p_t">共22条</span><span class="p_t">第</span>
      <span class="p_goto"><input class="p_goto_input" type="text" value="1"></span>
      <span class="p_t">/3页</span><span class="p_goto"><a href="javascript:;">跳转</a></span>
    </div>''','lxml').select_one('.pb_sys_common')


class InputCurrentPagerTests(unittest.TestCase):
    def next(self,node,*,tail=''):
        return _next_page_url('<main><h2>通知公告</h2>'+ITEMS+str(node)+tail+'</main>',
                              URL,1,list_selector='#items li')

    def test_observed_input_current_and_total_count_bind_the_next_link(self):
        node=pager()
        self.assertTrue(_counted_pager(node))
        self.assertEqual(self.next(node),'https://example.edu.cn/notices/2.htm')

    def test_a_different_count_still_follows_the_literal_next_address(self):
        node=pager();node.select('.p_t')[0].string='共37条';node.select('.p_t')[-1].string='/4页'
        self.assertTrue(_counted_pager(node))
        self.assertEqual(self.next(node),'https://example.edu.cn/notices/2.htm')

    def test_invalid_or_inconsistent_current_page_is_not_control_evidence(self):
        for value in ('0','4','n/a','2',''):
            with self.subTest(value=value):
                node=pager();node.select_one('input')['value']=value
                self.assertFalse(_counted_pager(node))
                self.assertIsNone(self.next(node))

    def test_missing_hidden_or_extra_page_field_cannot_supply_the_counter(self):
        for change in ('missing','hidden','hidden_attribute','extra'):
            with self.subTest(change=change):
                node=pager();field=node.select_one('input')
                if change=='missing':field.decompose()
                elif change=='hidden':field['type']='hidden'
                elif change=='hidden_attribute':field['hidden']=''
                else:field.parent.append(deepcopy(field))
                self.assertFalse(_counted_pager(node))
                self.assertIsNone(self.next(node))

    def test_a_foreign_article_in_the_counter_wrapper_is_not_hidden(self):
        node=pager();article=BeautifulSoup('<a href="/other.htm">相邻栏目真实文章</a>','lxml').a
        node.append(article)
        self.assertFalse(_counted_pager(node))
        self.assertIsNone(self.next(node))

    def test_duplicate_or_missing_disabled_current_marker_remains_ambiguous(self):
        for duplicate in (False,True):
            with self.subTest(duplicate=duplicate):
                node=pager();current=node.select_one('.p_no_d')
                if duplicate:current.parent.append(deepcopy(current))
                else:current['class']=['plain']
                self.assertFalse(_counted_pager(node))
                self.assertIsNone(self.next(node))

    def test_counter_under_another_heading_does_not_belong_to_the_first_list(self):
        neighbor='<section><h2>另一栏目</h2><ul><li><a href="/other.htm">另一栏目通知</a></li></ul>'+str(pager())+'</section>'
        self.assertIsNone(self.next('',tail=neighbor))


if __name__=='__main__':
    unittest.main()
