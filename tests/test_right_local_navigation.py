"""Bind current local navigation to the complete adjacent right-hand list."""
from copy import deepcopy
import unittest
from bs4 import BeautifulSoup

from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.discovery.listing_headings import zcms_right_local_heading
from backend.services.source_governance import _column_scope
from backend.services.source_review_recovery import observed_scope_revision

URL='https://www.example.edu.cn/office/notices/'


def layout(kind='new'):
    name='党务工作' if kind=='old' else '校区新闻' if kind=='second' else '招标公告'
    if kind=='second':
        active='SecondTop-nav__top--navClick'
        top='<div class="SecondTop-nav__top">'+''.join(
            f'<ul><li class="{active}"><a href="{URL}">{name}</a></li></ul>' for _ in range(3))+'</div>'
        left=f'<li class="SecondLeft-nav__left--navClick"><a href="{URL}">{name}</a></li>'
        cards=''.join(f'<div class="SecondList__list--list"><a href="/c/2026-01-0{i}/20000{i}.shtml"><img src="/thumb.png"></a>'
            f'<div><a class="SecondList__details--link" href="/c/2026-01-0{i}/20000{i}.shtml"><h3>关于本学期校区事务办理的通知{i}</h3></a>'
            f'<ul><li>2026-01-0{i}</li></ul></div></div>' for i in (1,2))
        listing='<div class="SecondList__list">'+cards+'<div class="SecondList__list--pagination">共2条记录，每页10条 第一页 | 上一页 | 下一页 | 最末页 第<input>页</div></div>'
        selector='.SecondList__list--list'
        title='a.SecondList__details--link'
        date='li'
    else:
        top=f'<div class="c-main__top"><ul><li class="middleTop__navClick"><a href="{URL}">{name}</a></li></ul></div>'
        left='' if kind=='old' else f'<li class="middleLeft__left--navClick"><a href="{URL}">{name}</a></li>'
        prefix='middleArticle' if kind=='old' else 'middleArticleNew'
        if kind=='old':
            cards=''.join(f'<div class="{prefix}__art--art"><div class="{prefix}__dates">2026-01-0{i}</div>'
                f'<a class="{prefix}__art--link" href="https://public.example.org/t2026010{i}_20000{i}.htm">'
                f'<h3 class="{prefix}__art--titles">关于学校项目采购工作的公告{i}</h3></a></div><hr>' for i in (1,2))
        else:
            cards=''.join(f'<div class="{prefix}__art--art"><div class="{prefix}__art--year">2026-01-0{i}</div>'
                f'<a href="https://public.example.org/t2026010{i}_20000{i}.htm"> <!-- empty image slot --> </a>'
                f'<a class="{prefix}__art--link" href="https://public.example.org/t2026010{i}_20000{i}.htm">关于学校项目采购工作的公告{i}</a></div><hr>' for i in (1,2))
        listing=f'<div class="{prefix}__art">{cards}</div><div class="middlePag">共2条记录，每页6条</div>'
        selector=f'.{prefix}__art--art'
        title=f'h3.{prefix}__art--titles' if kind=='old' else f'a.{prefix}__art--link'
        date=f'.{prefix}__dates' if kind=='old' else f'.{prefix}__art--year'
    html='<title>工学院</title><div class="c-main--center">'+top+'<div class="c-main__left"><ul>'+left+'</ul></div><div class="c-main__right">'+listing+'</div></div>'
    config=dict(name='旧配置名称',group_name='原显示分组',list_url=URL,list_selector=selector,
        title_selector=title,link_selector='a.middleArticle__art--link' if kind=='old' else title,
        date_selector=date,content_selector='')
    return html,config,name


class RightLocalNavigationTests(unittest.TestCase):
    def check_revision(self,kind):
        html,config,name=layout(kind)
        before=_column_scope(config,{'url':URL},html=html)[0]
        revision=observed_scope_revision(config,html)
        self.assertIsNotNone(revision)
        self.assertEqual(revision['config'],dict(config,name=name))
        records,errors,region=_column_scope(revision['config'],{'url':URL},revision['scope_evidence'],html=html)
        self.assertEqual((records,errors,region['name']),(before,[],name))
        return html,config,name

    def test_new_cards_keep_named_link_instead_of_empty_image_anchor(self):
        html,config,name=self.check_revision('new')
        feeds=publication_lists(html,URL)
        self.assertTrue(any(feed['name']==name and feed['item_count']==2 for feed in feeds))
        self.assertTrue(all(record['title'].startswith('关于学校项目采购')for record in _column_scope(config,{'url':URL},html=html)[0]))

    def test_old_cards_with_top_current_marker_do_not_assert_publisher(self):
        self.check_revision('old')

    def test_second_list_uses_agreeing_responsive_navigation_and_keeps_whole_list(self):
        self.check_revision('second')

    def test_sibling_thumbnail_requires_the_preceding_cards_exact_article_address(self):
        html,config,name=layout('old')
        soup=BeautifulSoup(html,'lxml')
        for card in soup.select('.middleArticle__art--art'):
            link=soup.new_tag('a',href=card.a['href'])
            link['class']=['middleArticle__art--img']
            card.insert_after(link)
        revision=observed_scope_revision(config,str(soup))
        self.assertIsNotNone(revision)
        soup.select_one('.middleArticle__art--img')['href']='/other-publication/'
        self.assertIsNone(observed_scope_revision(config,str(soup)))

    def test_plain_article_list_keeps_its_existing_heading_adapter(self):
        html,config,name=layout('old')
        soup=BeautifulSoup(html,'lxml')
        soup.title.string='通知公告'
        listing=soup.select_one('.middleArticle__art')
        listing.clear()
        listing.append(BeautifulSoup('<ul class="middleArticle__articleList">'+''.join(
            f'<li class="middleArticle--articleList"><a class="middleArticle__articleList--article" '
            f'href="/c/2026-01-0{i}/20000{i}.shtml">关于校区本学期相关事务的通知{i}</a>'
            f'<span class="middleArticle__articleList--date">2026-01-0{i}</span></li>'for i in(1,2))+'</ul>','lxml').ul)
        self.assertIsNone(zcms_right_local_heading(listing.li,URL))
        self.assertTrue(any(feed['name']=='通知公告'and feed['item_count']==2 for feed in publication_lists(str(soup),URL)))

    def test_conflicts_hidden_controls_and_neighbor_widgets_stay_unknown(self):
        for kind in ('new','old','second'):
            for mutation in ('foreign','duplicate','conflict','hidden','missing','neighbor','layout_neighbor','pager'):
                with self.subTest(kind=kind,mutation=mutation):
                    html,config,name=layout(kind)
                    soup=BeautifulSoup(html,'lxml')
                    controls=soup.select('.middleLeft__left--navClick,.middleTop__navClick,.SecondLeft-nav__left--navClick,.SecondTop-nav__top--navClick')
                    if mutation=='foreign':controls[0].a['href']='/other/'
                    elif mutation=='duplicate':
                        chosen=soup.select_one('.middleLeft__left--navClick,.SecondLeft-nav__left--navClick') or controls[0]
                        chosen.parent.append(deepcopy(chosen))
                    elif mutation=='conflict':
                        if len(controls)==1:
                            soup.select_one('.c-main__left ul').append(BeautifulSoup(
                                f'<li class="middleLeft__left--navClick"><a href="{URL}">另一个栏目</a></li>','lxml').li)
                        else:
                            controls[-1].a.string='另一个栏目'
                    elif mutation=='hidden':
                        for control in controls:control['hidden']=''
                    elif mutation=='missing':
                        for control in controls:control['class']=[]
                    elif mutation in ('neighbor','layout_neighbor'):
                        target=soup.select_one('.c-main__right' if mutation=='neighbor' else '.c-main--center')
                        target.append(BeautifulSoup('<section><h2>独立栏目</h2><a href="/other/">另一条信息</a></section>','lxml').section)
                    else:
                        soup.select_one('.middlePag,.SecondList__list--pagination').append('另一独立栏目')
                    self.assertIsNone(observed_scope_revision(config,str(soup)))

    def test_partial_selection_never_becomes_the_complete_column(self):
        for kind in ('new','old','second'):
            with self.subTest(kind=kind):
                html,config,name=layout(kind)
                config=dict(config,list_selector=config['list_selector']+':first-child')
                self.assertIsNone(observed_scope_revision(config,html))
