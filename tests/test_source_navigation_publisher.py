"""A current native source can use its own official homepage navigation.

Positive material is a declared actual CUPK homepage capture and existing
roster/config receipt. Mutations are semantic rejection guards only.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import gzip,hashlib,json,unittest
from backend.services.source_relationships import SourceRelationships
from backend.services.school_structure import _publisher_assertion

FIXTURES=Path(__file__).parent/'fixtures/structure'


class SourceNavigationPublisherTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((FIXTURES/'cupk-jwb-navigation-publisher-20261023.json').read_text('utf-8'))
        self.html=gzip.decompress((FIXTURES/self.data['body_file']).read_bytes()).decode('utf-8')
        self.assertEqual(hashlib.sha256(self.html.encode()).hexdigest(),self.data['body_sha256'])

    def relationships(self,data=None):
        data=data or self.data
        return SourceRelationships({'site':data['site'],'pages':data['pages']},data['records'],directory_navigation=True)

    def assertions(self,data=None,html=None):
        data=data or self.data;relationships=self.relationships(data);source=SimpleNamespace(**data['source'])
        owner=next(n for n in data['records'] if n['kind']=='unit' and n['relation']=='directory_entry')
        return [_publisher_assertion(path,source,owner,data['receipt'],
            lambda url:(self.html if html is None else html) if url==data['home'] else '',relationships)
            for path in relationships.paths_for(source.list_url)]

    def change_navigation(self,data,change):
        page=next(p for p in data['pages'] if p['url']==data['home'])
        for edge in page['navigation_edges']:
            change(edge)

    def test_actual_homepage_navigation_reaches_existing_textbook_source(self):
        paths=self.relationships().paths_for(self.data['source']['list_url'])
        self.assertTrue(paths,'The actual recorded homepage link must not disappear because it is not a model-labelled column')
        result=next((a for a in self.assertions() if a),None)
        self.assertIsNotNone(result)
        self.assertEqual(result['entry_kind'],'column_link')
        self.assertEqual(result['identity_reference_url'],self.data['home'])
        self.assertEqual(result['entry_reference_url'],self.data['home'])
        self.assertEqual(result['entry_url'],self.data['source']['list_url'])

    def test_candidate_navigation_without_a_receipt_does_not_grant_ownership(self):
        data=deepcopy(self.data);data['receipt']=None
        self.assertTrue(self.relationships(data).paths_for(data['source']['list_url']))
        self.assertFalse(any(self.assertions(data)))

    def test_stale_navigation_material_is_not_reused(self):
        data=deepcopy(self.data);self.change_navigation(data,lambda n:n.update(content_hash='stale'))
        self.assertFalse(self.relationships(data).paths_for(data['source']['list_url']))

    def test_missing_navigation_evidence_cannot_be_rebuilt_from_keywords(self):
        data=deepcopy(self.data)
        next(p for p in data['pages'] if p['url']==data['home'])['navigation_edges']=[]
        self.assertFalse(self.relationships(data).paths_for(data['source']['list_url']))

    def test_cross_unit_navigation_is_not_a_publisher_entrance(self):
        data=deepcopy(self.data);target='https://www.cupk.edu.cn/yjsb/tzgg/'
        self.change_navigation(data,lambda n:n.update(target_url=target))
        self.assertFalse(self.relationships(data).paths_for(target))

    def test_false_locator_is_rejected_by_original_html(self):
        data=deepcopy(self.data);self.change_navigation(data,lambda n:n.update(locator='footer a.nonexistent'))
        self.assertFalse(any(self.assertions(data)))

    def test_edge_from_a_different_parent_is_not_reused(self):
        data=deepcopy(self.data);self.change_navigation(data,lambda n:n.update(parent_url='https://www.cupk.edu.cn/'))
        self.assertFalse(self.relationships(data).paths_for(data['source']['list_url']))

    def test_edge_without_a_locator_is_not_a_path(self):
        data=deepcopy(self.data);self.change_navigation(data,lambda n:n.update(locator=''))
        self.assertFalse(self.relationships(data).paths_for(data['source']['list_url']))

    def test_failed_homepage_cannot_supply_navigation(self):
        data=deepcopy(self.data)
        next(p for p in data['pages'] if p['url']==data['home'])['state']='failed'
        self.assertFalse(self.relationships(data).paths_for(data['source']['list_url']))

    def test_changed_original_bytes_cannot_keep_the_old_proof(self):
        self.assertFalse(any(self.assertions(html=self.html+'changed')))

    def test_wrong_site_identity_cannot_supply_an_entrance(self):
        data=deepcopy(self.data);page=next(p for p in data['pages'] if p['url']==data['home'])
        page['title']='其他学院';notes=json.loads(page['notes_json']);page['notes_json']=json.dumps([
            n for n in notes if not n.startswith('page_branding_evidence:')],ensure_ascii=False)
        self.assertFalse(self.relationships(data).paths_for(data['source']['list_url']))


if __name__=='__main__':unittest.main()
