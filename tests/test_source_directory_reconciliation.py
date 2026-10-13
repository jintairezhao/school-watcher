import hashlib
import json
from types import SimpleNamespace
import unittest
from bs4 import BeautifulSoup

from backend.services.source_directory_reconciliation import (NAVIGATION_PREFIX,
    navigation_evidence,directory_websites,navigation_parent)
from backend.services.source_inventory import site_key

SCHOOL = SimpleNamespace(id=1,name='示例大学',url='https://example.edu.cn/')
PARENT = SimpleNamespace(id=10,kind='unit',school_id=1,name='学生工作部',list_url=SCHOOL.url+'students/')
CHILD = SimpleNamespace(id=20,kind='unit',school_id=1,name='学生资助管理中心',list_url='')
ENTRY = SimpleNamespace(parent_id=10,department_id=20)

def page(url,title,html):
    digest=hashlib.sha256(html.encode()).hexdigest()
    links=navigation_evidence(BeautifulSoup(html,'lxml'),url)
    evidence={'site_key':site_key(SCHOOL.url),'reference_url':url,'final_url':url,
              'content_hash':digest,'links':links}
    return {'url':url,'final_url':url,'title':title,'state':'fetched','kind':'unit',
            'content_hash':digest,'notes_json':json.dumps([NAVIGATION_PREFIX+json.dumps(evidence)])}

class DirectoryReconciliationTests(unittest.TestCase):
    def materials(self,container='nav',title='示例大学学生资助管理中心'):
        return [page(PARENT.list_url,'示例大学学生工作部',
                     f'<{container}><a href="/aid/">学生资助</a></{container}>'),
                page(SCHOOL.url+'aid/',title,'<header><a href="/students/">部门首页</a></header>')]

    def test_official_roster_and_reciprocal_navigation_locate_unlinked_child(self):
        result=directory_websites(SCHOOL,[PARENT,CHILD],[ENTRY],self.materials())
        self.assertEqual(result[20]['url'],SCHOOL.url+'aid/')
        self.assertEqual(result[20]['parent_id'],10)

    def test_footer_friendly_link_does_not_locate_child(self):
        self.assertEqual(directory_websites(SCHOOL,[PARENT,CHILD],[ENTRY],self.materials('footer')), {})

    def test_news_title_mention_and_missing_roster_are_not_ownership(self):
        self.assertEqual(directory_websites(SCHOOL,[PARENT,CHILD],[],self.materials()), {})
        self.assertEqual(directory_websites(SCHOOL,[PARENT,CHILD],[ENTRY],
                         self.materials(title='关于示例大学学生资助管理中心的通知')), {})

    def test_changed_snapshot_does_not_reuse_navigation_evidence(self):
        pages=self.materials();pages[0]['content_hash']='changed'
        self.assertEqual(directory_websites(SCHOOL,[PARENT,CHILD],[ENTRY],pages), {})

    def test_one_way_link_is_not_sufficient_to_fill_unlinked_unit(self):
        pages=self.materials();pages[1]['notes_json']='[]'
        self.assertEqual(directory_websites(SCHOOL,[PARENT,CHILD],[ENTRY],pages), {})

    def test_primary_navigation_preserves_browsing_path_without_inventing_subunit(self):
        source=SimpleNamespace(name='学生工作部-学生资助',list_url=SCHOOL.url+'aid/notices/')
        pages=self.materials();pages[1]['notes_json']='[]'
        result=navigation_parent(source,SCHOOL,[PARENT,CHILD],pages)
        self.assertEqual(result,{'unit_id':10,'name':'学生资助'})
        self.assertEqual(directory_websites(SCHOOL,[PARENT,CHILD],[ENTRY],pages), {})

    def test_legacy_prefix_or_friendly_link_alone_is_not_a_browsing_path(self):
        source=SimpleNamespace(name='学生工作部-学生资助',list_url=SCHOOL.url+'aid/notices/')
        pages=self.materials('footer');pages[1]['notes_json']='[]'
        self.assertIsNone(navigation_parent(source,SCHOOL,[PARENT,CHILD],pages))
        pages=self.materials();pages[0]['title']='另一个部门';pages[1]['notes_json']='[]'
        self.assertIsNone(navigation_parent(source,SCHOOL,[PARENT,CHILD],pages))

    def test_school_home_link_does_not_make_every_prefixed_source_a_department_column(self):
        source=SimpleNamespace(name='学生工作部-未知来源',list_url=SCHOOL.url+'unrelated/notices/')
        pages=[page(PARENT.list_url,'示例大学学生工作部',
                    '<nav><a href="/">学校首页</a></nav>')]
        self.assertIsNone(navigation_parent(source,SCHOOL,[PARENT,CHILD],pages))

    def test_shared_domain_does_not_join_rosters_of_different_schools(self):
        other=SimpleNamespace(id=10,kind='unit',school_id=2,name=PARENT.name,list_url=PARENT.list_url)
        self.assertEqual(directory_websites(SCHOOL,[other,CHILD],[ENTRY],self.materials()), {})

    def test_introduction_page_header_is_not_a_unit_website(self):
        pages=self.materials();notes=json.loads(pages[1]['notes_json'])
        notes.append('unit_profile_page:observed-introduction');pages[1]['notes_json']=json.dumps(notes)
        self.assertEqual(directory_websites(SCHOOL,[PARENT,CHILD],[ENTRY],pages), {})

if __name__ == '__main__': unittest.main()
