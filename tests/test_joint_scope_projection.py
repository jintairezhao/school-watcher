"""A preview and a complete installed column keep distinct source identities."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from backend.services import source_governance as g
from backend.services.source_scope import observe_joint_selection
from backend.services.structure_facts import build_structure_facts

ROOT='https://school.example.edu.cn/'
TITLE={'a':'通知公告','b':'校区动态'}
def rows(key,count=3):
    return ''.join(f'<li><a href="/info/{key}{i}.htm">关于本学期{TITLE[key]}事项的通知{i}</a><span>2026-10-25</span></li>' for i in range(count))
HOME=''.join(f'<section id="p{key}"><h2>{TITLE[key]}</h2><a href="/{key}/">更多</a><ul>{rows(key)}</ul></section>' for key in TITLE)
BODIES={ROOT:HOME,**{ROOT+key+'/':f'<section id="list"><h2>{TITLE[key]}</h2><ul>{rows(key,4)}</ul></section>' for key in TITLE}}
BASE=dict(title_selector='a',link_selector='a',date_selector='span',content_selector='article',group_name='')
SOURCE=dict(id=7,school_id=1,kind='column',name='原联合订阅',list_url=ROOT,list_selector='#pa li, #pb li',**BASE)
TARGETS=[dict(id=8+i,school_id=1,kind='column',name=TITLE[key],list_url=ROOT+key+'/',list_selector='#list li',**BASE) for i,key in enumerate(TITLE)]
SITE=dict(id=1,name='示例大学',root_url=ROOT,site_key='school')
PAGES=[dict(url=url,state='fetched',content_hash=g._hash(html),checked_at='2026-10-25T08:00:00+00:00') for url,html in BODIES.items()]


class JointScopeProjectionTests(unittest.TestCase):
    def setUp(self):
        self.scopes=[dict(observe_joint_selection(g.source_config(type('Row',(),SOURCE)()),
            dict(url=ROOT,hash=g._hash(HOME),observed_at=PAGES[0]['checked_at']),HOME),source_id=7)]
        self.departments=deepcopy([SOURCE,*TARGETS])
        self.receipts=[dict(source_id=t['id'],url=t['list_url'],config_hash=g._hash({f:t[f] for f in g.FIELDS}),
                            receipt_id='native:'+str(t['id'])) for t in TARGETS]

    def resolved(self,**changes):
        from backend.services.source_scope_projection import resolve_scope_components
        data=dict(scopes=self.scopes,pages=PAGES,departments=self.departments,receipts=self.receipts,material=BODIES.get)
        data.update(changes)
        return resolve_scope_components(**data)

    def test_actual_links_and_executed_entries_resolve_without_merging_or_granting_receipt(self):
        result=self.resolved()
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['selected_items'],6)
        self.assertEqual({c['target_source_id'] for c in result[0]['components']},{8,9})
        self.assertTrue(all(c['status']=='resolved' and not c['full_column_equivalent'] for c in result[0]['components']))
        facts=build_structure_facts(site=SITE,pages=PAGES,departments=self.departments,directory_entries=[],
            structure_nodes=[],navigation_edges=[],receipts=self.receipts,source_scopes=result)
        self.assertEqual({n['id'] for n in facts['nodes'] if n['entity_type']=='source'},{'source:7','source:8','source:9'})
        source=next(n for n in facts['nodes'] if n['id']=='source:7')
        self.assertEqual(source['scope_kind'],'configured_multiple_regions')
        self.assertEqual(source['verification'],'unknown')
        edges=[r for r in facts['relations'] if r['type']=='source_preview']
        self.assertEqual({r['target'] for r in edges},{'source:8','source:9'})
        self.assertFalse(any(r['type']=='source_ownership' for r in facts['relations']))

    def test_unverified_missing_material_and_missing_items_stay_explicit_gaps(self):
        unverified=self.resolved(receipts=[])[0]
        self.assertTrue(all(c['status']=='target_unverified' for c in unverified['components']))
        missing=self.resolved(material=lambda url:BODIES.get(url) if url!=ROOT+'a/' else '')[0]
        self.assertIn('target_material_missing',[c['status'] for c in missing['components']])
        body=BODIES[ROOT+'a/'].replace('/info/a0.htm','/info/new.htm')
        pages=deepcopy(PAGES);next(p for p in pages if p['url']==ROOT+'a/')['content_hash']=g._hash(body)
        changed=self.resolved(pages=pages,material=lambda url:body if url==ROOT+'a/' else BODIES.get(url))[0]
        self.assertIn('preview_items_not_confirmed',[c['status'] for c in changed['components']])

    def test_same_url_and_name_multiple_candidates_require_identity_resolution(self):
        duplicate=dict(TARGETS[0],id=10)
        receipts=self.receipts+[dict(self.receipts[0],source_id=10,receipt_id='native:10')]
        result=self.resolved(departments=self.departments+[duplicate],receipts=receipts)[0]
        component=next(c for c in result['components'] if c['name']==TITLE['a'])
        self.assertEqual(component['status'],'target_identity_ambiguous')
        self.assertNotIn('target_source_id',component)

    def test_old_source_config_scope_cannot_apply_after_rule_change(self):
        self.departments[0]['list_selector']='#pa li'
        self.assertEqual(self.resolved(),[])

    def graph(self, states=None, *, scope='connected', revision=1, version=None):
        from backend.services.structure_snapshot import graph_projection
        departments=[dict(row,lifecycle=(states or {}).get(row['id'],'active')) for row in self.departments]
        facts=build_structure_facts(site=SITE,pages=PAGES,departments=departments,directory_entries=[],
            structure_nodes=[],navigation_edges=[],receipts=self.receipts,source_scopes=self.resolved())
        capture=dict(facts=facts,report=dict(site=SITE,pages=PAGES),department_records=departments,
            version=version or dict(epoch='joint-lifecycle-fixture',revision=revision),
            attention_policy=dict(profiles=['undergraduate','graduate','teacher'],weights={}))
        return graph_projection(capture,SimpleNamespace(id=1,url=ROOT,name=SITE['name']),scope=scope)

    def test_inactive_complete_column_does_not_narrow_or_remove_legacy_union(self):
        for state in ('disabled','removed'):
            with self.subTest(state=state):
                graph=self.graph({8:state})
                nodes={n['id']:n for n in graph['nodes']}
                self.assertIn('source:7',nodes)
                self.assertIn('source:9',nodes)
                self.assertNotIn('source:8',nodes)
                self.assertEqual(nodes['source:7']['source_scope']['selected_items'],6)
                self.assertEqual(len(nodes['source:7']['source_scope']['components']),2)
                self.assertEqual(nodes['source:7']['verification'],'unknown')
                self.assertTrue(all(r['source'] in nodes and r['target'] in nodes for r in graph['relations']))
                previews=[r for r in graph['relations'] if r['type']=='source_preview']
                self.assertEqual({r['target'] for r in previews},{'source:9'})
                explorer={n['id']:n for n in self.graph({8:state},scope='exploration')['nodes']}
                self.assertEqual(explorer['source:8']['state'],state)

    def test_removing_legacy_union_keeps_both_complete_column_identities(self):
        graph=self.graph({7:'removed'})
        sources={n['id'] for n in graph['nodes'] if n['entity_type']=='source'}
        self.assertEqual(sources,{'source:8','source:9'})
        self.assertFalse(any(r['type']=='source_preview' for r in graph['relations']))
        restored=self.graph()
        self.assertEqual({n['id'] for n in restored['nodes'] if n['entity_type']=='source'},
            {'source:7','source:8','source:9'})
        self.assertFalse(any(r['type']=='source_ownership' for r in restored['relations']))

    def test_preview_lifecycle_deltas_reconstruct_views_without_changing_source_scope(self):
        from backend.services.structure_snapshot import transport_update
        from backend.services.structure_versions import current_version
        from backend.database.db import db
        from backend.database.models import Department
        from backend.database.source_lifecycle_models import SourceLifecycle
        from tests import test_structure_versions as versions
        fixture=versions.StructureVersionTests()
        fixture.setUp()
        try:
            self.assertEqual(fixture.sid,1)
            fixture.school.url=ROOT
            db.session.add_all(Department(**row) for row in self.departments)
            db.session.commit()
            previous=transport_update(self.graph(version=current_version(fixture.sid)),school_id=fixture.sid)
            for state in ('disabled','removed','active'):
                row=db.session.get(SourceLifecycle,8)
                if row is None:
                    row=SourceLifecycle(source_id=8,school_id=fixture.sid,state=state)
                    db.session.add(row)
                else:row.state=state
                db.session.commit()
                states={} if state=='active' else {8:state}
                target=self.graph(states,version=current_version(fixture.sid))
                update=transport_update(target,school_id=fixture.sid,since=previous['structure_token'])
                self.assertEqual(update['update_mode'],'delta')
                for field in ('nodes','relations'):
                    rows={r['id']:r for r in previous[field]}
                    for ident in update['delta'][field]['remove']:rows.pop(ident,None)
                    rows.update({r['id']:r for r in update['delta'][field]['upsert']})
                    self.assertEqual(rows,{r['id']:r for r in target[field]})
                previous=transport_update(target,school_id=fixture.sid)
        finally:
            fixture.tearDown()
        self.assertEqual(self.departments,[SOURCE,*TARGETS])
        self.assertEqual(self.scopes[0]['selected_items'],6)


if __name__=='__main__':unittest.main()
