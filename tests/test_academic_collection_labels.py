"""A catalogue must reach the first layout pass regardless of spelling."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from backend.scraper.discovery.structure import classify, directory_document
from backend.scraper.discovery.navigation_order import order_candidates
from backend.scraper.discovery.frontier import choose_route
from backend.services.student_sources import college_priority


class AcademicCollectionLabelTests(unittest.TestCase):
    def test_plural_college_centre_collection_does_not_name_an_individual_centre(self):
        for label in ('院系中心','院系、中心','院系与中心','院系/中心'):
            with self.subTest(label=label):
                self.assertEqual(classify(label),'directory')
                self.assertTrue(directory_document(label,'unit'))
        for label in ('数学研究中心','院系中心通知','院系中心工作动态','中心介绍'):
            with self.subTest(label=label):
                self.assertNotEqual(classify(label),'directory')

    def test_parsed_collection_keeps_directory_type_through_queue_and_initial_claim(self):
        from backend.scraper.discovery.structure import extract_structure
        from backend.services.source_inventory import Inventory
        root='https://fixture.example.edu/'
        html=('<nav><a href="/a-organizations">组织机构</a>'
              '<a href="/b-site-map">网站地图</a>'
              '<a href="/z-colleges">院系中心</a>'
              '<a href="/centre">数学研究中心</a></nav>')
        material=hashlib.sha256(html.encode()).hexdigest()
        parsed=extract_structure(html,root,root,'root','测试大学',[])
        link=next(row for row in parsed['links'] if row['url']==root+'z-colleges')
        self.assertEqual(link['kind'],'directory')
        directory_nodes=[row for row in parsed['nodes'] if row.get('url')==link['url']]
        self.assertTrue(directory_nodes)
        self.assertTrue(all(row['kind']!='unit' for row in directory_nodes))
        pairs=[(row,row['label']) for row in parsed['links']]
        before=copy.deepcopy(pairs)
        self.assertEqual(order_candidates(pairs,{'kind':'root'})[0][0]['url'],link['url'])
        self.assertEqual(pairs,before)
        with tempfile.TemporaryDirectory(prefix='collection-type-') as folder:
            store=Inventory(Path(folder)/'inventory.sqlite3')
            key=store.ensure_site('测试大学',root)
            store.finish(key,root,state='fetched')
            with store.connect() as connection:
                connection.execute('UPDATE pages SET content_hash=? WHERE site_key=? AND url=?',
                                   (material,key,root))
            for row in parsed['links']:
                store.enqueue(key,row['url'],row['label'],row['kind'],1,[], 'school_domain')
            store.record_edges(key,root,[dict(row,decision='follow') for row in parsed['links']],material)
            stored=store.get_page(key,link['url'])
            self.assertEqual(stored['kind'],'directory')
            self.assertEqual(Inventory(store.path).claim(key,focus='valuable')['url'],link['url'])

    def test_generic_collection_forms_are_consistent_across_parser_and_scheduling(self):
        for label in ('学院（部）','学院(系)','院（系）','院(系)','学部与学院','学院/学部',
                      '学院、系','各学院','全部院系','学院一览','Schools & Departments',
                      'Faculties and Schools'):
            with self.subTest(label=label):
                self.assertEqual(classify(label),'directory')
                self.assertTrue(directory_document(label,'unit'))
                self.assertEqual(college_priority('directory',label),0)

    def test_individual_college_news_and_post_title_are_not_collection_labels(self):
        for label in ('化工学院','经济学院（国际部）','学院新闻','研究生院','学院简介',
                      'School of Chemistry','关于学院（部）工作的通知'):
            with self.subTest(label=label):
                self.assertNotEqual(classify(label),'directory')
                self.assertFalse(directory_document(label,'unit'))

    def test_real_root_catalogue_hint_precedes_research_without_rewriting_old_paid_candidate(self):
        pairs=[({'url':'https://u.example/research','kind':'directory','label':'科研机构'},'科研机构'),
               ({'url':'https://u.example/colleges','kind':'unit','label':'学院（部）'},'学院（部）')]
        before=copy.deepcopy(pairs)
        self.assertEqual(order_candidates(pairs,{'kind':'root'})[0][0]['label'],'学院（部）')
        self.assertEqual(pairs,before)

    def test_existing_catalogue_frontier_keeps_its_real_root_edge_and_trunk_turn(self):
        root={'url':'https://u.example/','kind':'root','state':'fetched','content_hash':'a'*64}
        pages=[{'url':root['url']+'research','kind':'directory','label':'科研机构','depth':1},
               {'url':root['url']+'colleges','kind':'unit','label':'学院（部）','depth':1}]
        edges=[{'parent_url':root['url'],'target_url':p['url'],'kind':p['kind'],
                'content_hash':root['content_hash'],'decision':'follow'} for p in pages]
        selected=choose_route(pages,edges=edges,parents={root['url']:root})
        self.assertEqual(selected['label'],'学院（部）')
        stale=[dict(e,content_hash='b'*64) for e in edges]
        reasons=[]
        choose_route(pages,edges=stale,parents={root['url']:root},explain=reasons.append)
        self.assertNotEqual(reasons[-1]['reason'],'observed_homepage_layout')


if __name__=='__main__':unittest.main()
