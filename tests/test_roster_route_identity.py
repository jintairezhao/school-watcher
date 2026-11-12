"""A route hint cannot rewrite a directory's observed entity identity."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import unittest

from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.layered import apply_route,route_structure
from backend.services.discovery_changes import compare,put

FIXTURES=Path(__file__).parent/'fixtures/structure'
ROOT='https://www.cupk.edu.cn/'


class RosterRouteIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data=json.loads((FIXTURES/'cupk-service-website-bindings-20261023.json').read_text(encoding='utf-8'))
        cls.url=next(url for url in data['materials'] if url.endswith('/485097.shtml'))
        record=data['materials'][cls.url]
        cls.html=gzip.decompress((FIXTURES/record['file']).read_bytes()).decode()
        assert hashlib.sha256(cls.html.encode()).hexdigest()==record['sha256']

    def parse(self):
        return extract_structure(self.html,self.url,ROOT,'directory','组织机构',[])

    def unit_and_link(self,parsed):
        node=next(n for n in parsed['nodes'] if n['name']=='研究生部' and n['kind']=='unit')
        link=next(l for l in parsed['links'] if l['url']==node['url'] and l.get('locator')==node['locator'])
        return node,link

    def test_actual_directory_identity_survives_every_navigation_role(self):
        for kind,role in [('gateway','gateway'),('channel','publishing'),('unit','unit')]:
            with self.subTest(role=role):
                parsed=self.parse();node,link=self.unit_and_link(parsed);before=deepcopy(node)
                child_parents=[n.get('parent') for n in parsed['nodes']]
                apply_route(parsed,link,dict(kind=kind,decision='follow',ai_reviewed=True,ai_role=role))
                self.assertEqual(node,before)
                self.assertEqual([n.get('parent') for n in parsed['nodes']],child_parents)
                self.assertEqual(link['kind'],kind,'Routing remains separate from the observed entity')

    def test_cached_role_replay_preserves_the_same_roster_identity(self):
        page=dict(url=self.url,kind='directory',label='组织机构',path_json='[]',notes_json='[]')
        parsed=self.parse();route_structure(parsed,page,self.html,ROOT,policy='valuable')
        node,link=self.unit_and_link(parsed);before=deepcopy(node)
        previous=compare(page,parsed,None,self.html,self.url)
        previous['routes']=[dict(url=link['url'],label=link['label'],kind='gateway',decision='follow',ai_reviewed=True,ai_role='gateway')]
        page['notes_json']=json.dumps(put([],previous))
        replay=self.parse();route_structure(replay,page,self.html,ROOT,policy='valuable')
        result=compare(page,replay,None,self.html,self.url)
        self.assertEqual(result['change'],'unchanged')
        self.assertEqual(next(n for n in replay['nodes'] if n['locator']==before['locator']),before)

    def test_ordinary_navigation_role_remains_navigation_evidence(self):
        from backend.services.source_relationships import ROSTER_RELATIONS
        html='<title>测试大学</title><nav><a href="/office/">办事入口</a></nav>'
        parsed=extract_structure(html,ROOT,ROOT,'root','测试大学',[])
        link=next(l for l in parsed['links'] if l['url']==ROOT+'office/')
        apply_route(parsed,link,dict(kind='unit',decision='follow',ai_reviewed=True,ai_role='unit'))
        nodes=[n for n in parsed['nodes'] if n.get('url')==link['url']]
        self.assertTrue(nodes)
        self.assertTrue(all(n['relation'] not in ROSTER_RELATIONS for n in nodes))


if __name__=='__main__':unittest.main()
