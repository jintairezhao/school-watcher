"""A named card's date and synopsis do not belong to its article headline."""
import unittest
from bs4 import BeautifulSoup
from backend.scraper.detectors.title_quality import extract_title_text
from backend.services.source_governance import _extract

URL='https://example.edu.cn/finance/notices/'


class NamedCardTitleFieldsTests(unittest.TestCase):
    def html(self):
        return '<li><div class="list_item"><a href="/finance/c/2026-06-10/534693.shtml">' \
            '<p class="p1">关于试用期满考核结果的公示</p><p class="p2">2026-06-10</p>' \
            '<p class="p3 list_item_des">根据学校规定，现将本次考核结果予以公示……</p></a></div></li>'

    def test_explicit_title_field_excludes_date_and_synopsis(self):
        soup=BeautifulSoup(self.html(),'lxml')
        self.assertEqual(extract_title_text(soup.a,anchor=soup.a,page_url=URL),'关于试用期满考核结果的公示')
        config=dict(name='通告公告',list_url=URL,list_selector='li',title_selector='a',
            link_selector='a',date_selector='',content_selector='',group_name='财务部')
        _,_,records,errors=_extract(config,{'url':URL},html=self.html())
        self.assertEqual(errors,[])
        self.assertEqual(records[0]['title'],'关于试用期满考核结果的公示')

    def test_generic_paragraph_classes_do_not_invent_a_headline(self):
        soup=BeautifulSoup(self.html().replace('class="list_item"','class="ordinary-text"'),'lxml')
        self.assertIn('2026-06-10',extract_title_text(soup.a,anchor=soup.a,page_url=URL))
        soup=BeautifulSoup(self.html().replace('p2','other-field'),'lxml')
        self.assertIn('2026-06-10',extract_title_text(soup.a,anchor=soup.a,page_url=URL))


if __name__=='__main__':unittest.main()
