"""A source's own exact branding can witness its already-recorded publisher.

The positive case uses real retained CUPK material, candidate path and receipt.
Mutated cases check evidence rejection, not real-world university behavior.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import gzip
import hashlib
import json
import unittest

from backend.services.school_structure import _publisher_assertion
from backend.services.source_relationships import SourceRelationships

FIXTURES=Path(__file__).parent/'fixtures/structure'


class PublisherSourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((FIXTURES/'cupk-smxy-publisher-20261014.json').read_text('utf-8'))
        encoded=(FIXTURES/self.data['body_file']).read_bytes()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),self.data['container_sha256'])
        body=gzip.decompress(encoded)
        self.assertEqual(hashlib.sha256(body).hexdigest(),self.data['body_sha256'])
        self.html=body.decode('utf-8')

    def check_assertion(self,data=None,html=None):
        data=deepcopy(data or self.data)
        relationships=SourceRelationships(dict(site=data['site'],pages=data['pages']),[])
        available=set(data['available_body_urls'])
        return _publisher_assertion(data['path'],SimpleNamespace(**data['source']),data['owner'],
            data['receipt'],lambda url:(self.html if html is None else html) if url in available else '',relationships)

    def test_retained_source_branding_does_not_require_a_second_homepage_body(self):
        self.assertNotIn(self.data['owner']['url'],self.data['available_body_urls'])
        assertion=self.check_assertion()
        self.assertIsNotNone(assertion,'The exact source page, roster path and original receipt are sufficient')
        self.assertIn(assertion['identity_reference_url'],self.data['available_body_urls'])
        self.assertEqual(assertion['identity_text'],'中国石油大学（北京）克拉玛依校区马克思主义学院')
        self.assertEqual(assertion['entry_kind'],'publication_region')
        self.assertEqual(assertion['entry_url'],self.data['source']['list_url'])

    def test_missing_receipt_stays_unknown(self):
        data=deepcopy(self.data);data['receipt']=None
        self.assertIsNone(self.check_assertion(data))

    def test_directory_placement_alone_stays_unknown(self):
        data=deepcopy(self.data);data['path']['basis']='official_directory_navigation'
        self.assertIsNone(self.check_assertion(data))

    def test_modified_bytes_cannot_reuse_the_old_hash(self):
        self.assertIsNone(self.check_assertion(html=self.html.replace('马克思主义学院','教务部')))

    def test_a_unit_mentioned_in_an_article_title_is_not_exact_branding(self):
        data=deepcopy(self.data)
        html=self.html.replace('<title>中国石油大学（北京）克拉玛依校区马克思主义学院</title>',
            '<title>马克思主义学院举办教学研讨活动</title>')
        self.assertNotEqual(html,self.html)
        for page in data['pages']:
            if page['url'] in data['available_body_urls']:
                page.update(title='马克思主义学院举办教学研讨活动',content_hash=hashlib.sha256(html.encode()).hexdigest(),notes_json='[]')
        for reference in data['path']['references']:
            if reference['url'] in data['available_body_urls']:
                reference['content_hash']=hashlib.sha256(html.encode()).hexdigest()
        self.assertIsNone(self.check_assertion(data,html))

    def test_profile_page_branding_does_not_establish_publication_ownership(self):
        data=deepcopy(self.data)
        for page in data['pages']:
            if page['url'] in data['available_body_urls']:
                page['notes_json']=json.dumps(['unit_profile_page:retained-profile'])
        self.assertIsNone(self.check_assertion(data))

    def test_exact_branding_for_another_unit_does_not_assign_the_candidate_owner(self):
        data=deepcopy(self.data)
        html=self.html.replace('中国石油大学（北京）克拉玛依校区马克思主义学院',
            '中国石油大学（北京）克拉玛依校区教务部')
        digest=hashlib.sha256(html.encode()).hexdigest()
        for page in data['pages']:
            if page['url'] in data['available_body_urls']:
                page.update(title='中国石油大学（北京）克拉玛依校区教务部',content_hash=digest,notes_json='[]')
        for reference in data['path']['references']:
            if reference['url'] in data['available_body_urls']:
                reference['content_hash']=digest
        data['receipt']['content_hash']=digest
        self.assertIsNone(self.check_assertion(data,html))

    def test_fresh_bytes_do_not_rescue_mismatched_published_metadata(self):
        data=deepcopy(self.data)
        for page in data['pages']:
            if page['url'] in data['available_body_urls']:
                page['content_hash']='a-different-published-version'
        self.assertIsNone(self.check_assertion(data))

    def test_a_cross_unit_source_address_cannot_use_the_candidate_website(self):
        data=deepcopy(self.data)
        data['source']['list_url']='https://www.cupk.edu.cn/jwb/tzgg/'
        self.assertIsNone(self.check_assertion(data))


if __name__=='__main__':unittest.main()
