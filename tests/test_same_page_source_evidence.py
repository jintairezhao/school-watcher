"""Actual native source 169 keeps its observed widget evidence across views."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import unittest

from backend.services.structure_facts import build_structure_facts
from backend.services.structure_snapshot import graph_projection, source_choices, source_paths


FIXTURE = Path(__file__).parent / 'fixtures/structure/swpu-source169-region-20261102.json'


class SamePageSourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.data = deepcopy(self.fixture['inputs'])

    def capture(self):
        from backend.services.discovery_policy import get_attention_policy
        site = self.data['site']
        school = SimpleNamespace(id=site['id'], name=site['name'], url=site['root_url'])
        facts = build_structure_facts(**self.data)
        capture = dict(facts=facts, report=dict(pages=self.data['pages'], exploration_events=[]),
            department_records=self.data['departments'],
            version={k: self.fixture['provenance']['structure_version'][k] for k in ('epoch', 'revision')},
            source_configurations=[], model_executions=[], attention_policy=get_attention_policy())
        return school, capture

    def test_actual_source_keeps_one_identity_and_the_same_web_proofs_in_all_views(self):
        school, capture = self.capture()
        graph = graph_projection(capture, school)
        source, = [n for n in graph['nodes'] if n['entity_type'] == 'source']
        self.assertEqual((source['id'], source['label'], source['url'], source['verification']),
                         ('source:169', '通知公告', self.data['receipts'][0]['url'], 'verified'))
        edge, = [r for r in graph['relations'] if r['target'] == 'source:169']
        proofs = [p for p in edge['evidence'] if p['kind'] == 'web_observation']
        self.assertEqual({p['locator'] for p in proofs},
                         {self.data['receipts'][0]['region'][key] for key in ('container', 'heading')})
        self.assertTrue(all(p['content_hash'] == self.data['receipts'][0]['content_hash'] for p in proofs))
        references = source_paths(capture)[169][0]['references']
        self.assertTrue({(p['locator'], p['content_hash']) for p in proofs}.issubset(
            {(r['locator'], r['material']) for r in references}))
        choices = source_choices(capture)
        self.assertEqual(set(choices), {'栏目（归属待核实）'})
        column = choices['栏目（归属待核实）'][0]['own_columns'][0]
        self.assertEqual((column['department'].id, column['label'], column['url']),
                         (source['source_id'], source['label'], source['url']))
        self.assertEqual(graph['structure_version'], capture['version'])
        self.assertFalse(any(r['type'] in ('source_ownership', 'organization_membership') for r in graph['relations']))

    def test_refresh_and_duplicate_receipt_do_not_duplicate_nodes_or_proofs(self):
        school, capture = self.capture()
        first = graph_projection(capture, school)
        self.data['receipts'] *= 2
        school, again = self.capture()
        second = graph_projection(again, school)
        self.assertEqual(first, second)
        self.assertEqual(len({n['id'] for n in second['nodes']}), len(second['nodes']))

    def test_unavailable_original_keeps_configuration_but_does_not_assert_web_proof(self):
        target = self.data['receipts'][0]['url']
        next(p for p in self.data['pages'] if p['url'] == target)['material_available'] = False
        _, capture = self.capture()
        edge, = [r for r in capture['facts']['relations'] if r['target'] == 'source:169']
        self.assertEqual({p['kind'] for p in edge['evidence']}, {'source_configuration'})
        self.assertIn('original_material_not_retained', [g['code'] for g in capture['facts']['gaps']])


if __name__ == '__main__':
    unittest.main()
