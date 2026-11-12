"""Literal .tit fields are headlines; event details remain separate card data."""
import unittest
from bs4 import BeautifulSoup

from backend.scraper.detectors.title_quality import extract_title_text, extract_best_title
from backend.scraper.discovery.publication_lists import samples_for
from backend.services.source_governance import _extract

URL='https://example.edu.cn/'


def card(title,index=1):
    return (f'<li><a class="con" href="/info/1001/{100000+index}.htm"><div class="alltxts">'
        f'<div class="pic"><img src="/poster.png"></div><div class="txts">'
        f'<div class="tit txtof2">{title}</div><div class="info">'
        '<div class="item txtof1">2026年10月13日 8:30—9:30</div>'
        '<div class="item txtof1 adress">主楼822</div></div></div></div>'
        '<div class="date"><div class="dt">10 / 10</div></div></a></li>')


class AbbreviatedHeadlineFieldTests(unittest.TestCase):
    def test_discovery_and_formal_extraction_use_the_same_literal_headline(self):
        titles=['【活动预告】数学与工程应用讲座','关于开展本科生课程选课的通知']
        html='<ul>'+''.join(card(t,i)for i,t in enumerate(titles,1))+'</ul>'
        soup=BeautifulSoup(html,'lxml');config={'list_selector':'li','title_selector':'a',
            'link_selector':'a','date_selector':''}
        discovered,_,_=samples_for(soup.select('li'),config,URL)
        _,_,formal,errors=_extract(config,{'url':URL},html=html)
        self.assertFalse(errors)
        self.assertEqual([r['title']for r in discovered],titles)
        self.assertEqual([r['title']for r in formal],titles)
        self.assertEqual([r['url']for r in formal],[r['url']for r in discovered])
        self.assertIn('2026年10月13日',soup.get_text())
        self.assertIn('主楼822',soup.get_text())

    def test_legitimate_year_ranges_and_numbers_stay_in_the_headline(self):
        for title in ('2026—2027学年课程教学安排通知','5G实验室开放通知','50—13:20实验编号说明'):
            item=BeautifulSoup(card(title),'lxml').li
            with self.subTest(title=title):
                self.assertEqual(extract_title_text(item.a,anchor=item.a),title)
                self.assertEqual(extract_title_text(item.select_one('.tit'),anchor=item.a),title)

    def test_two_different_headline_fields_remain_ambiguous(self):
        item=BeautifulSoup('<a href="/info/1.htm"><div class="tit">项目一报名通知</div>'
            '<div class="tit">项目二报名通知</div></a>','lxml').a
        self.assertEqual(extract_title_text(item,anchor=item),'')
        self.assertEqual(extract_best_title(item,article_url=item['href']),'')

    def test_another_article_cannot_supply_the_abbreviated_headline(self):
        item=BeautifulSoup('<li><a href="/info/1.htm"><img src="/one.png"></a>'
            '<a href="/info/2.htm"><div class="tit">另一个项目报名通知</div></a></li>','lxml').li
        self.assertEqual(extract_title_text(item,anchor=item.a),'')
        self.assertEqual(extract_best_title(item,article_url=item.a['href']),'')


if __name__=='__main__':unittest.main()
