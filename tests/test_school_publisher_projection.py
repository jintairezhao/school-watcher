"""School identity, current execution and original DOM jointly prove a publisher."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from backend.services.source_governance import _hash
from backend.services.structure_facts import build_structure_facts

ROOT = 'https://school.example.edu.cn/'
URL = ROOT+'notices/'
NAME = 'Example University'
HOME = '<title>'+NAME+'</title><a href="/notices/">通知公告</a>'
HTML = '<title>'+NAME+'</title><section id="notices"><h2>通知公告</h2><ul>'+''.join(
    '<li><a href="/info/'+str(i)+'.htm">关于本学期课程安排的通知'+str(i)+
    '</a><span>2026-10-25</span></li>' for i in range(3))+'</ul></section>'
SITE = dict(id=1, name=NAME, root_url=ROOT, site_key='example-school')
CONFIG = dict(name='通知公告',list_url=URL,list_selector='#notices li',title_selector='a',link_selector='a',
    date_selector='span',content_selector='article',group_name=NAME)
DEPARTMENT = dict(id=7,school_id=1,kind='column',lifecycle='active',**CONFIG)
PAGES = [dict(url=u,state='fetched',content_hash=hashlib.sha256(h.encode()).hexdigest(),
    checked_at='2026-10-25T08:00:00+00:00') for u,h in ((ROOT,HOME),(URL,HTML))]
RECEIPT = dict(source_id=7,url=URL,content_hash=PAGES[1]['content_hash'],config_hash=_hash(CONFIG),
               receipt_id='proposal:1',region=dict(name='通知公告',heading='#notices h2',container='#notices'))


class SchoolPublisherProjectionTests(unittest.TestCase):
    def gate(self, home=HOME, column=HTML, *, reference_url=URL, config=None):
        from backend.services.source_workflow import school_publisher_proven
        bundle = dict(root_url=ROOT, school_name=NAME,
            list=dict(url=reference_url, html=column),
            publisher_material={'home': dict(url=ROOT, html=home)})
        with patch('backend.services.source_governance.read_snapshot', side_effect=lambda ref: ref['html']):
            return school_publisher_proven(bundle, CONFIG if config is None else config)

    def observations(self, *, pages=None, departments=None, receipts=None, bodies=None):
        from backend.services.school_publishers import observe_school_publishers
        return observe_school_publishers(site=SITE,pages=PAGES if pages is None else pages,
            departments=[DEPARTMENT] if departments is None else departments,
            receipts=[RECEIPT] if receipts is None else receipts,
            material=(bodies or {ROOT:HOME,URL:HTML}).get)

    def build(self, proofs, **changes):
        data=dict(site=SITE,pages=PAGES,structure_nodes=[],navigation_edges=[],departments=[DEPARTMENT],
                  directory_entries=[],receipts=[RECEIPT],school_publishers=proofs)
        data.update(changes)
        return build_structure_facts(**data)

    def test_school_uses_its_own_stable_identity_and_provenance_not_department_row(self):
        proofs=self.observations()
        self.assertEqual(len(proofs),1)
        facts=self.build(proofs)
        school=next(n for n in facts['nodes'] if n.get('organization_kind')=='school')
        self.assertEqual((school['id'],school['school_id'],school['label']),('school:1',1,NAME))
        self.assertNotIn('department_id',school)
        relation,=[r for r in facts['relations'] if r['type']=='source_ownership']
        self.assertEqual((relation['source'],relation['target']),('school:1','source:7'))
        self.assertEqual(len(relation['evidence']),3)
        self.assertFalse(any(r['type']=='organization_membership' for r in facts['relations']))
        self.assertTrue(any(r['type']=='entity_page' and r['source']=='school:1' for r in facts['relations']))
        self.assertEqual(self.build(proofs),facts)

    def test_activation_and_projection_use_the_same_complete_school_identity(self):
        self.assertTrue(self.gate())
        self.assertTrue(self.observations())
        for title in (NAME+' Chemistry College', 'Other '+NAME, NAME+' Alumni Association'):
            column=HTML.replace('<title>'+NAME+'</title>', '<title>'+title+'</title>')
            pages=deepcopy(PAGES);pages[1]['content_hash']=hashlib.sha256(column.encode()).hexdigest()
            with self.subTest(title=title):
                self.assertFalse(self.gate(column=column))
                self.assertFalse(self.observations(pages=pages,bodies={ROOT:HOME,URL:column}))

    def test_activation_and_projection_require_the_same_branded_home_and_real_link(self):
        for home in (HOME.replace('<title>'+NAME+'</title>',''),
                     HOME.replace('/notices/','/elsewhere/'),
                     '<base href="https://other.example.edu.cn/">'+HOME):
            pages=deepcopy(PAGES);pages[0]['content_hash']=hashlib.sha256(home.encode()).hexdigest()
            with self.subTest(home=home):
                self.assertFalse(self.gate(home=home))
                self.assertFalse(self.observations(pages=pages,bodies={ROOT:home,URL:HTML}))
        self.assertFalse(self.gate(reference_url=ROOT+'other-material/'))

    def test_activation_and_projection_do_not_ignore_a_column_base_element(self):
        column='<base href="https://other.example.edu.cn/">'+HTML
        pages=deepcopy(PAGES);pages[1]['content_hash']=hashlib.sha256(column.encode()).hexdigest()
        self.assertFalse(self.gate(column=column))
        self.assertFalse(self.observations(pages=pages,bodies={ROOT:HOME,URL:column}))

    def test_blank_display_group_does_not_discard_proven_school_ownership(self):
        config=dict(CONFIG,group_name='')
        department=dict(DEPARTMENT,group_name='')
        receipt=dict(RECEIPT,config_hash=_hash(config))
        self.assertTrue(self.gate(config=config))
        proofs=self.observations(departments=[department],receipts=[receipt])
        self.assertEqual(len(proofs),1)
        facts=self.build(proofs,departments=[department],receipts=[receipt])
        ownership,=[r for r in facts['relations'] if r['type']=='source_ownership']
        self.assertEqual((ownership['source'],ownership['target']),('school:1','source:7'))
        self.assertEqual(next(n for n in facts['nodes'] if n['id']=='source:7')['display_group'],'')

    def test_receipt_group_branding_and_actual_column_scope_cannot_be_replaced_by_url(self):
        self.assertEqual(self.observations(receipts=[]),[])
        for field,value in [('config_hash','0'*64),('url',ROOT+'elsewhere/'),('source_id',8)]:
            receipt=dict(RECEIPT,**{field:value})
            self.assertEqual(self.observations(receipts=[receipt]),[])
        for group in ('组织机构', 'Other School'):
            department=dict(DEPARTMENT,group_name=group)
            self.assertEqual(self.observations(departments=[department]),[])
        for replacement in (HTML.replace(NAME,NAME+' Chemistry College'), HTML.replace('通知公告</h2>','学院新闻</h2>')):
            pages=deepcopy(PAGES);pages[1]['content_hash']=hashlib.sha256(replacement.encode()).hexdigest()
            self.assertEqual(self.observations(pages=pages,bodies={ROOT:HOME,URL:replacement}),[])

    def test_missing_changed_or_failed_material_and_withdrawn_direct_entry_do_not_grant_owner(self):
        self.assertEqual(self.observations(bodies={ROOT:HOME}),[])
        self.assertEqual(self.observations(bodies={ROOT:HOME,URL:HTML+'changed'}),[])
        pages=deepcopy(PAGES);pages[0]['state']='failed'
        self.assertEqual(self.observations(pages=pages),[])
        home=HOME.replace('/notices/','/other/')
        pages=deepcopy(PAGES);pages[0]['content_hash']=hashlib.sha256(home.encode()).hexdigest()
        self.assertEqual(self.observations(pages=pages,bodies={ROOT:home,URL:HTML}),[])

    def test_projection_refuses_proof_for_other_school_config_receipt_or_page_version(self):
        good=self.observations()
        for field,value in [('school_id',2),('school_name','Other School'),('config_hash','0'*64),
                            ('receipt_id','proposal:unknown'),('root_url',ROOT+'other/')]:
            proof=dict(good[0],**{field:value})
            self.assertFalse(any(r['type']=='source_ownership' for r in self.build([proof])['relations']))
        stale=deepcopy(good);stale[0]['evidence']['column_identity']['content_hash']='0'*64
        self.assertFalse(any(r['type']=='source_ownership' for r in self.build(stale)['relations']))


class SchoolPublisherSnapshotTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_governance import SourceGovernanceTests
        from backend.database.db import db
        from backend.services import source_governance as g
        self.f=SourceGovernanceTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.school,self.source=self.f.school,self.f.dept
        self.source.list_url=self.school.url+'notices/'
        self.source.group_name=self.school.name
        db.session.commit()
        self.guard=patch('socket.socket.connect',side_effect=AssertionError('No network'))
        self.guard.start();self.addCleanup(self.guard.stop)
        html='<title>'+self.school.name+'</title>'+self.f.html()
        home=g._snapshot('<title>'+self.school.name+'</title><a href="/notices/">通知公告</a>',self.school.url,role='identity')
        captured=g.capture_source_evidence(self.school.id,g.source_config(self.source),department_id=self.source.id,
            seed_html=html,fetcher=self.f.fetcher(html),publisher_material={'school_home':home})
        self.proposal=g.propose_source(self.school.id,g.source_config(self.source),captured,department_id=self.source.id)
        self.assertTrue(g.validate_proposal(self.proposal.id)['passed'])
        g.activate_proposal(self.proposal.id)
        self.bundle=json.loads(self.proposal.evidence_json)
        self.client=self.f.app.test_client()
        with self.client.session_transaction() as session:session.update(user_id=self.f.admin.id,_csrf_token='school-owner')
        self.endpoint=f'/api/schools/{self.school.id}/structure-snapshot'

    def graph(self,**query):
        response=self.client.get(self.endpoint,query_string=query)
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        return response.json

    def test_list_path_and_graph_agree_without_creating_department(self):
        from backend.database.models import Department
        from backend.services.structure_snapshot import read_structure,source_choices,source_paths
        count=Department.query.count()
        capture=read_structure(self.school);graph=self.graph()
        owner=next(n for n in graph['nodes'] if n.get('organization_kind')=='school')
        self.assertEqual(owner['id'],f'school:{self.school.id}')
        self.assertEqual(owner['kind_label'],'学校')
        group=next(group for groups in source_choices(capture).values() for group in groups
                   if any(c['department'].id==self.source.id for c in group['own_columns']))
        self.assertEqual((group['key'],group['name']),(owner['id'],self.school.name))
        paths=source_paths(capture)[self.source.id]
        self.assertTrue(any(p['relation_type']=='source_ownership' and p['nodes'][0]['name']==self.school.name for p in paths))
        self.assertEqual(Department.query.count(),count)

    def test_inactive_current_graph_and_config_change_keep_stable_source_identity(self):
        from backend.database.db import db
        from backend.services.source_lifecycle import set_source_state
        source_id=f'source:{self.source.id}'
        for state in ('disabled','removed'):
            set_source_state(self.source,state);db.session.commit()
            self.assertFalse(any(n['id']==source_id for n in self.graph()['nodes']))
        set_source_state(self.source,'active');db.session.commit()
        original=self.source.list_selector
        self.source.list_selector='#other li';db.session.commit()
        self.assertFalse(any(r['type']=='source_ownership' for r in self.graph()['relations']))
        self.source.list_selector=original;db.session.commit()
        self.assertTrue(any(r['target']==source_id and r['type']=='source_ownership' for r in self.graph()['relations']))

    def test_failed_newest_validation_and_recovery_do_not_borrow_an_old_receipt(self):
        from backend.database.db import db
        from backend.database.source_governance_models import SourceValidation
        from backend.services import source_governance as g
        for passed in (False,True):
            detail=dict(status='full' if passed else 'rejected',articles={'passed':passed},
                errors=[] if passed else ['article_mismatch'],region=self.bundle['region'],list_snapshot=self.bundle['list'])
            db.session.add(SourceValidation(school_id=self.school.id,department_id=self.source.id,
                config_hash=g._hash(g.source_config(self.source)),validation_json=json.dumps(detail)))
            db.session.commit()
            self.assertEqual(any(r['type']=='source_ownership' for r in self.graph()['relations']),passed)

    def test_projection_upgrade_resets_cached_view_then_refreshes_with_empty_delta(self):
        from backend.services import structure_snapshot as snapshots
        with patch('backend.services.school_publishers.observe_school_publishers',return_value=[]),\
                patch.object(snapshots,'STRUCTURE_PROJECTION_VERSION',1):
            old=self.graph()
        upgraded=self.graph(since=old['structure_token'])
        self.assertEqual(upgraded['update_mode'],'snapshot')
        self.assertEqual(upgraded['structure_version'],old['structure_version'])
        self.assertEqual(upgraded['structure_projection_version'],snapshots.STRUCTURE_PROJECTION_VERSION)
        refreshed=self.graph(since=upgraded['structure_token'])
        self.assertEqual(refreshed['update_mode'],'delta')
        self.assertFalse(any(v for r in refreshed['delta'].values() for v in r.values()))


if __name__=='__main__':unittest.main()
