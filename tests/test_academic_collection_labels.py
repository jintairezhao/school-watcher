"""A catalogue must reach the first layout pass regardless of spelling."""
import copy
import unittest

from backend.scraper.discovery.structure import classify, directory_document
from backend.scraper.discovery.navigation_order import order_candidates
from backend.scraper.discovery.frontier import choose_route
from backend.services.student_sources import college_priority


class AcademicCollectionLabelTests(unittest.TestCase):
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
