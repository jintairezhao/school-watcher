"""A marked local self-link may name its sole complete adjacent list."""
from copy import deepcopy
import unittest
from bs4 import BeautifulSoup

from backend.scraper.discovery.publication_lists import publication_lists
from backend.services.source_governance import _column_scope
from backend.services.source_review_recovery import observed_scope_revision

FINANCE_URL='https://www.cupk.edu.cn/cwb/xzzx/'
COLLEGE_URL='https://www.cupk.edu.cn/gxy/xsgz/zytz/'
FINANCE='''<div class="c-main--center"><div class="c-main__left"><ul>
<li class="middleLeft__left--navClick"><a href="/cwb/xzzx/">下载中心</a></li></ul></div>
<div class="c-main__right"><div id="list_container" class="list_box">
<li><a href="/cwb/c/2026-01-01/100001.shtml"><p class="p1">经费报销表格下载</p><p class="p2">2026-01-01</p></a></li>
<li><a href="/cwb/c/2026-01-02/100002.shtml"><p class="p1">项目报销办理指南</p><p class="p2">2026-01-02</p></a></li>
</div><div class="middlePag"><ul class="c-pagination"></ul></div></div></div>'''
COLLEGE='''<div class="c-main u-clearfix"><div class="c-main--center"><div class="c-main--top"><ul>
<li><a href="/gxy/xsgz/tpxw/">图片新闻</a></li>
<li class="middleTop__navClick"><a href="/gxy/xsgz/zytz/">重要通知</a></li>
<li><a href="/gxy/xsgz/xwkd/">新闻快递</a></li></ul></div>
<div class="c-main--bottom"><div class="middleArticle__art u-clearfix">
<ul class="middleArticle__artList"><a href="/gxy/c/2026-01-01/200001.shtml"><li class="middleArticle__artList--article">关于本学期学生事务办理流程及注意事项的通知</li></a><li class="middleArticle__artList--date">2026-01-01</li></ul>
<ul class="middleArticle__artList"><a href="/gxy/c/2026-01-02/200002.shtml"><li class="middleArticle__artList--article">关于做好本学期学院学生工作与安全教育的通知</li></a><li class="middleArticle__artList--date">2026-01-02</li></ul>
</div><div class="middlePag">共 12 条记录，每页6条 <a href="index_2.shtml">下一页</a> 第<input value="1">页</div></div></div></div>'''

def config(url,selector,name):
    return dict(name=name,group_name='组织机构',list_url=url,list_selector=selector,title_selector='a',
        link_selector='a',date_selector='',content_selector='')

class LocalNavigationColumnTests(unittest.TestCase):
    def test_finance_sidebar_provides_observed_name_without_changing_source_selection(self):
        original=config(FINANCE_URL,'#list_container > li','财务部-下载中心')
        revision=observed_scope_revision(original,FINANCE)
        self.assertIsNotNone(revision)
        self.assertEqual(revision['config'],dict(original,name='下载中心'))
        rows,errors,region=_column_scope(revision['config'],{'url':FINANCE_URL},revision['scope_evidence'],html=FINANCE)
        self.assertEqual((len(rows),errors,region['name']),(2,[],'下载中心'))

    def test_college_selected_top_navigation_names_only_its_sibling_list(self):
        original=config(COLLEGE_URL,'.middleArticle__art > ul','学院-重要通知')
        revision=observed_scope_revision(original,COLLEGE)
        self.assertIsNotNone(revision)
        self.assertEqual(revision['config'],dict(original,name='重要通知'))
        rows,errors,_=_column_scope(revision['config'],{'url':COLLEGE_URL},revision['scope_evidence'],html=COLLEGE)
        self.assertEqual((len(rows),errors),(2,[]))

    def test_missing_duplicate_foreign_or_hidden_current_links_stay_unknown(self):
        for html,url,marker in [(FINANCE,FINANCE_URL,'.middleLeft__left--navClick'),(COLLEGE,COLLEGE_URL,'.middleTop__navClick')]:
            for mutation in ('missing','duplicate','foreign','hidden'):
                with self.subTest(url=url,mutation=mutation):
                    soup=BeautifulSoup(html,'lxml');active=soup.select_one(marker)
                    if mutation=='missing':active['class']=[]
                    elif mutation=='duplicate':active.parent.append(deepcopy(active))
                    elif mutation=='foreign':active.a['href']='/other/'
                    else:active['hidden']=''
                    self.assertTrue(all(not f['name'] for f in publication_lists(str(soup),url)))

    def test_neighboring_widget_or_foreign_pager_text_does_not_borrow_identity(self):
        for html,url,content in [(FINANCE,FINANCE_URL,'.c-main__right'),(COLLEGE,COLLEGE_URL,'.c-main--bottom')]:
            for mutation in ('neighbor','pager'):
                with self.subTest(url=url,mutation=mutation):
                    soup=BeautifulSoup(html,'lxml')
                    if mutation=='neighbor':soup.select_one(content).append(BeautifulSoup('<section><h2>另一栏目</h2><a href="/foreign/">其他内容</a></section>','lxml').section)
                    else:soup.select_one('.middlePag').append('独立学术活动栏目')
                    self.assertTrue(all(not f['name'] for f in publication_lists(str(soup),url)))

    def test_partial_selector_cannot_be_repaired_into_a_complete_column(self):
        for html,url,selector,name in [(FINANCE,FINANCE_URL,'#list_container > li:first-child','下载中心'),
                (COLLEGE,COLLEGE_URL,'.middleArticle__art > ul:first-child','重要通知')]:
            with self.subTest(url=url):
                original=config(url,selector,name)
                self.assertIsNone(observed_scope_revision(original,html))
                self.assertIn('column_identity_or_scope_unconfirmed',_column_scope(original,{'url':url},html=html)[1])
