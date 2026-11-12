"""Actual roster units may have a separately linked official service website."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import gzip
import hashlib
import json
import unittest

from backend.services.school_structure import _publisher_assertion
from backend.services.source_relationships import SourceRelationships
from backend.services.structure_facts import build_structure_facts

FIXTURES=Path(__file__).parent/'fixtures/structure'


class ServiceWebsiteBindingTests(unittest.TestCase):
    def setUp(self):
        self.data=json.loads((FIXTURES/'cupk-service-website-bindings-20261023.json').read_text('utf-8'))
        self.materials={}
        for url,record in self.data['materials'].items():
            html=gzip.decompress((FIXTURES/record['file']).read_bytes()).decode('utf-8')
            self.assertEqual(hashlib.sha256(html.encode()).hexdigest(),record['sha256'])
            self.materials[url]=html

    def relationships(self,data=None):
        data=data or self.data
        return SourceRelationships({'site':data['site'],'pages':data['pages']},data['records'],directory_navigation=True)

    def source(self,ident,data=None):
        return SimpleNamespace(**next(s for s in (data or self.data)['sources'] if s['id']==ident))

    def assertions(self,ident,data=None,materials=None):
        data=data or self.data;relationships=self.relationships(data);source=self.source(ident,data)
        receipt=next((r for r in data['receipts'] if r['source_id']==ident),None)
        return [_publisher_assertion(path,source,next(n for n in reversed(path['nodes']) if n['kind']=='unit'),
            receipt,lambda url:(materials or self.materials).get(url,''),relationships)
            for path in relationships.paths_for(source.list_url)]

    def test_actual_aid_sources_bind_to_the_rostered_center_without_a_homepage_url(self):
        before=deepcopy(self.data)
        for ident in (1441,1442):
            paths=self.relationships().paths_for(self.source(ident).list_url)
            self.assertTrue(paths)
            self.assertEqual({p['unit_name'] for p in paths},{'学生资助管理中心'})
            self.assertEqual({p['website_url'] for p in paths},{'https://www.cupk.edu.cn/xszz/'})
            self.assertTrue(all(not p['nodes'][-1]['url'] for p in paths),'A website binding must not rewrite the roster identity')
            self.assertTrue(any(self.assertions(ident)))
        self.assertEqual(self.data,before)

    def test_actual_management_site_is_another_website_of_the_same_existing_unit(self):
        paths=self.relationships().paths_for(self.source(1607).list_url)
        self.assertTrue(paths)
        self.assertEqual({p['unit_name'] for p in paths},{'学生工作与安全保卫部'})
        self.assertEqual({p['nodes'][-1]['url'] for p in paths},{'https://www.cupk.edu.cn/xgybw/'})
        self.assertTrue(any(self.assertions(1607)))

    def test_website_placement_does_not_replace_the_current_source_receipt(self):
        data=deepcopy(self.data);data['receipts']=[]
        self.assertTrue(self.relationships(data).paths_for(self.source(1441,data).list_url))
        self.assertFalse(any(self.assertions(1441,data)))

    def test_different_roster_row_cannot_supply_a_website(self):
        data=deepcopy(self.data)
        for n in data['records']:
            if n['name']=='学生资助' and n['relation']=='table_reference':n['parent_key']='unrelated-row'
        self.assertFalse(self.relationships(data).paths_for(self.source(1441,data).list_url))

    def test_expired_directory_material_cannot_bind_a_website(self):
        data=deepcopy(self.data)
        for n in data['records']:
            if n['relation']=='table_reference':n['content_hash']='expired'
        self.assertFalse(self.relationships(data).paths_for(self.source(1441,data).list_url))

    def test_no_header_identity_is_not_replaced_by_topic_words(self):
        data=deepcopy(self.data);page=next(p for p in data['pages'] if p['url']=='https://www.cupk.edu.cn/xszz/')
        page.update(title='学生资助消息',notes_json='[]')
        self.assertFalse(self.relationships(data).paths_for(self.source(1441,data).list_url))

    def test_false_directory_link_locator_is_rejected_against_the_original(self):
        data=deepcopy(self.data)
        for n in data['records']:
            if n['name']=='学生资助' and n['relation']=='table_reference':n['locator']='a.no-such-entry'
        self.assertFalse(any(self.assertions(1441,data)))

    def test_false_unit_name_location_does_not_bind_an_unrelated_unit(self):
        data=deepcopy(self.data)
        for n in data['records']:
            if n['name']=='学生资助管理中心':n['locator']='title'
        self.assertFalse(any(self.assertions(1441,data)))

    def test_changed_directory_bytes_revoke_the_binding(self):
        materials=dict(self.materials);url=next(u for u in materials if '/c/2017-' in u)
        materials[url]+='changed'
        self.assertFalse(any(self.assertions(1441,materials=materials)))

    def test_profile_page_cannot_become_a_whole_website(self):
        data=deepcopy(self.data);page=next(p for p in data['pages'] if p['url']=='https://www.cupk.edu.cn/xszz/')
        page['notes_json']=json.dumps(json.loads(page['notes_json'])+['unit_profile_page:review'])
        self.assertFalse(self.relationships(data).paths_for(self.source(1441,data).list_url))

    def projection_data(self):
        relationships=self.relationships()
        directory=next(u for u in self.materials if '/c/2017-' in u)
        work=next(n for n in self.data['records'] if n['name']=='学生工作与安全保卫部')
        center=next(n for n in self.data['records'] if n['name']=='学生资助管理中心')
        units=[dict(id=1001,school_id=1,name=work['name'],kind='unit',list_url=work['url']),
               dict(id=1002,school_id=1,name=center['name'],kind='unit',list_url='')]
        def proofs(binding):
            return [dict(reference_url=url,locator=loc,content_hash=relationships.pages[url]['content_hash'],
                observed_at=relationships.pages[url]['checked_at']) for url,loc in (
                    (directory,binding['unit_locator']),(directory,binding['entry_locator']),
                    (binding['website_url'],binding['identity_locator']))]
        entries=[]
        for ident,node in ((1001,work),(1002,center)):
            base=dict(parent_id=1001,department_id=ident,facts_json=[dict(type='directory_listing',
                node_key=node['node_key'],evidence=[dict(reference_url=directory,locator=node['locator'],
                    content_hash=node['content_hash'],observed_at=node['observed_at'])])])
            for binding in relationships.website_bindings:
                if binding['unit_key']==node['node_key']:
                    base['facts_json'].append(dict(type='entity_page',writer='official_structure',
                        node_key=node['node_key'],page_url=binding['website_url'],assertion=binding,
                        observed_relation='verified_directory_website',evidence=proofs(binding)))
            entries.append(base)
        return dict(site=dict(self.data['site'],id=1),pages=deepcopy(self.data['pages']),
            structure_nodes=deepcopy(self.data['records']),navigation_edges=[],departments=units,
            directory_entries=entries)

    def website_links(self,data):
        return [r for r in build_structure_facts(**data)['relations']
                if r['type']=='entity_page' and r.get('observed_relation')=='verified_directory_website']

    def test_page_bindings_survive_without_source_receipts_and_do_not_create_admin_relations(self):
        data=self.projection_data();result=build_structure_facts(**data)
        self.assertEqual(len(self.website_links(data)),2)
        self.assertFalse(any(r['type']=='organization_membership' for r in result['relations']))
        self.assertEqual({n['department_id'] for n in result['nodes'] if n['entity_type']=='organization'},{1001,1002})

    def test_one_existing_unit_keeps_its_original_site_and_another_page_binding(self):
        data=self.projection_data();result=build_structure_facts(**data);by={n['id']:n for n in result['nodes']}
        work=next(n['id'] for n in result['nodes'] if n.get('department_id')==1001)
        self.assertEqual({by[r['target']]['url'] for r in result['relations'] if r['source']==work and r['type']=='entity_page'},
            {'https://www.cupk.edu.cn/xgybw/','https://www.cupk.edu.cn/xsgl/'})

    def test_current_website_material_is_required_by_the_projection(self):
        data=self.projection_data()
        for page in data['pages']:
            if page['url']=='https://www.cupk.edu.cn/xszz/':page['content_hash']='newer-observation'
        self.assertEqual(len(self.website_links(data)),1)

    def test_same_named_different_identity_cannot_take_the_roster_binding(self):
        data=self.projection_data();data['departments'].append(dict(data['departments'][1],id=1003))
        row=next(e for e in data['directory_entries'] if e['department_id']==1002)
        fact=next(f for f in row['facts_json'] if f['type']=='entity_page')
        row['facts_json'].remove(fact)
        data['directory_entries'].append(dict(parent_id=1001,department_id=1003,facts_json=[fact]))
        self.assertFalse(any(r['assertions'][0]['unit_name']=='学生资助管理中心' for r in self.website_links(data)))

    def test_another_unit_key_cannot_be_substituted_in_a_valid_binding(self):
        data=self.projection_data()
        for row in data['directory_entries']:
            for fact in row['facts_json']:
                if fact['type']=='entity_page':fact['assertion']['unit_key']='another-roster-identity'
        self.assertFalse(self.website_links(data))

    def test_navigation_writer_cannot_promote_a_page_binding(self):
        data=self.projection_data()
        for row in data['directory_entries']:
            for fact in row['facts_json']:
                if fact['type']=='entity_page':fact['writer']='navigation'
        self.assertFalse(self.website_links(data))

    def test_display_placement_uses_the_bound_site_without_rewriting_the_unit(self):
        from backend.services.source_placements import placements_from_paths
        source=self.source(1441)
        paths=self.relationships().paths_for(source.list_url)
        places=placements_from_paths(source,self.data['site']['root_url'],paths,{'组织机构'})
        self.assertEqual(len(places),1)
        self.assertEqual(places[0]['nodes'][-1]['name'],'学生资助管理中心')
        self.assertEqual(places[0]['nodes'][-1]['url'],'https://www.cupk.edu.cn/xszz/')
        self.assertEqual(paths[0]['nodes'][-1]['url'],'')


if __name__=='__main__':unittest.main()
