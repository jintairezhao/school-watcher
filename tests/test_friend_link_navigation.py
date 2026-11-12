"""Link collections prove access, not roster or source ownership."""
from copy import deepcopy
import gzip
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from backend.scraper.discovery import structure
from backend.services.source_relationships import ROSTER_RELATIONS

ROOT='https://www.neu.edu.cn/'
PAGE='http://aao.neu.edu.cn/2320/list.htm'
FIXTURE=Path(__file__).parent/'fixtures/structure/neu-aao-friend-links-39607d18-20261104.html.gz'


class FriendLinkNavigationTests(unittest.TestCase):
    def test_actual_select_links_keep_access_evidence_without_directory_membership(self):
        raw=gzip.decompress(FIXTURE.read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
            '39607d181361f9c66d430b4abb728c1a38506c94d8a8b6b876c2394070c543ff')
        soup=BeautifulSoup(raw.decode(),'lxml')
        positions={structure.locator(n)for n in soup.select('select option')}
        parsed=structure.extract_structure(raw.decode(),PAGE,ROOT,'directory','本科专业设置')
        units=[n for n in parsed['nodes']if n['kind']=='unit'and n['locator']in positions]
        self.assertEqual(len(units),22)
        self.assertTrue(all(n['relation']=='navigation_entry'for n in units))
        self.assertFalse(any(n['relation']=='directory_group'and n['name']=='友情链接'for n in parsed['nodes']))
        for node in units:
            self.assertTrue(any(link['url']==node['url']and link['locator']==node['locator']for link in parsed['links']))

    def test_same_named_real_body_member_and_friend_copy_stay_distinct(self):
        html=('<main><h2>院系设置</h2><select><option value="/college/">计算机学院</option></select></main>'
            '<aside><h3>友情链接</h3><select><option value="/college/">计算机学院</option></select></aside>')
        parsed=structure.extract_structure(html,ROOT+'schools/',ROOT,'directory','院系设置')
        units=[n for n in parsed['nodes']if n['name']=='计算机学院']
        self.assertEqual({n['relation']for n in units},{'directory_entry','navigation_entry'})
        self.assertEqual(len({n['locator']for n in units}),2)
        self.assertEqual(len({n['key']for n in units}),1)

    def test_unit_keeps_its_own_column_but_not_a_named_link_collection_column(self):
        for title in ('友情链接','相关链接','Related Links'):
            with self.subTest(title=title):
                html=('<nav><a href="/notices/">通知公告</a></nav><div><h3>'+title+
                    '</h3><a href="https://central.neu.edu.cn/notices/">学校通知</a></div>')
                parsed=structure.extract_structure(html,ROOT+'college/',ROOT,'unit','计算机学院')
                nodes={n['name']:n for n in parsed['nodes']}
                self.assertEqual(nodes['通知公告']['relation'],'unit_channel')
                self.assertEqual(nodes['学校通知']['relation'],'navigation_entry')


class FriendLinkReconciliationTests(unittest.TestCase):
    def test_reparse_retires_only_automatic_legacy_membership_and_keeps_navigation_in_graph(self):
        from tests import test_school_structure as fixture
        from backend.database.db import db
        from backend.database.models import Department,DepartmentDirectoryEntry,Subscription
        from backend.scraper.discovery.inventory_crawler import inspect_page
        from backend.services.structure_snapshot import read_structure,graph_projection
        f=fixture.SchoolStructureTests();f.setUp();self.addCleanup(f.tearDown)
        directory=fixture.ROOT+'colleges/'
        f.pages[directory]=('<main><h1>院系设置</h1><a href="'+fixture.CS+'">计算机学院</a></main>'
            '<div><h3>友情链接</h3><select><option value="'+fixture.LANG+'">外国语学院</option>'
            '<option value="'+fixture.ROOT+'library/">图书馆</option></select></div>')
        original=structure.auxiliary_navigation
        def legacy_auxiliary(element):
            if structure.nearby_heading(element)[0]=='友情链接':return False
            return original(element)
        with patch.object(structure,'auxiliary_navigation',side_effect=legacy_auxiliary):
            f.publish()
        parent=Department.query.filter_by(name='友情链接',kind='group').one()
        false_units={d.name:d for d in Department.query.filter(Department.name.in_(['外国语学院','图书馆']),Department.kind=='unit')}
        self.assertEqual(set(false_units),{'外国语学院','图书馆'})
        old_ids={d.name:d.id for d in Department.query.all()}
        source=Department(school_id=f.school.id,name='已存通知',kind='column',list_url=fixture.LANG+'notices/',list_selector='#notices li')
        db.session.add(source);db.session.flush()
        subscription=Subscription.query.one();subscription.department_ids=[source.id]
        source_id=source.id;subscription_before=list(subscription.department_ids)
        for name,unit in false_units.items():
            row=DepartmentDirectoryEntry.query.filter_by(parent_id=parent.id,department_id=unit.id).one()
            inherited=deepcopy(row.facts_json[0]);inherited['writer']='directory_options'
            row.facts_json=[*row.facts_json,inherited]
            if name=='外国语学院':
                row.facts_json=[*row.facts_json,dict(writer='manual_test',type='cross_entity_reference',note='keep independently maintained fact')]
        db.session.commit()
        inspect_page(f.inventory,f.inventory.report(f.key)['site'],f.inventory.get_page(f.key,directory),
            fetcher=lambda address:dict(url=address,status=200,html=f.pages[address]),ai_assist=False)
        f.catalog.publish(f.inventory,f.key)
        retained=DepartmentDirectoryEntry.query.filter_by(parent_id=parent.id,department_id=false_units['外国语学院'].id).one()
        self.assertEqual(retained.facts_json,[dict(writer='manual_test',type='cross_entity_reference',note='keep independently maintained fact')])
        self.assertIsNone(DepartmentDirectoryEntry.query.filter_by(parent_id=parent.id,department_id=false_units['图书馆'].id).first())
        self.assertEqual(db.session.get(Department,source_id).list_selector,'#notices li')
        self.assertEqual(Subscription.query.one().department_ids,subscription_before)
        self.assertTrue(all(db.session.get(Department,ident).name==name for name,ident in old_ids.items()))
        capture=read_structure(f.school)
        graph=graph_projection(capture,f.school,scope='exploration')
        for facts in (capture['facts'],graph):
            self.assertTrue(any(edge['type']=='navigation_link'and any(
                p['reference_url']==directory and 'option:'in p['locator'] for p in edge['evidence'])
                for edge in facts['relations']))
            self.assertFalse(any(edge['type']in ('directory_listing','organization_membership','source_ownership')
                and any(p.get('reference_url')==directory and 'option:'in p.get('locator','')for p in edge['evidence'])
                for edge in facts['relations']))
        units=[d.name for d in Department.query.filter_by(kind='unit')]
        self.assertIn('计算机学院',units)

