"""A validated proposal publishes and reads the same executed list observation."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.services import source_governance as governance
from backend.services.source_path_receipts import load_source_receipts
from backend.services.structure_snapshot import read_structure, graph_projection, source_paths
from tests import test_source_governance as fixture


class ProposalPublicationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.SourceGovernanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.school, self.source = self.fixture.school, self.fixture.dept

    def capture(self, *, changed_independent=False):
        original = self.fixture.fetcher(self.fixture.html())
        def fetch(url, purpose, **kwargs):
            html = original(url, purpose, **kwargs)
            return '<header>学校服务</header>' + html if changed_independent and purpose == 'independent_list' else html
        return governance.capture_source_evidence(self.school.id, governance.source_config(self.source),
            department_id=self.source.id, inventory=self.fixture.identity, fetcher=fetch)

    def save(self, evidence):
        proposal = governance.propose_source(self.school.id, governance.source_config(self.source),
            evidence, department_id=self.source.id)
        validation = governance.validate_proposal(proposal.id)
        self.assertTrue(validation['passed'], validation)
        with patch('requests.sessions.Session.send', side_effect=AssertionError('No network')), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No model')):
            governance.activate_proposal(proposal.id)
        return proposal, validation

    def receipt(self):
        rows = load_source_receipts(dict(name=self.school.name, root_url=self.school.url))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_validation_preserves_executed_region_when_package_omits_it(self):
        evidence = self.capture()
        evidence.bundle.pop('region')
        proposal, validation = self.save(evidence)
        publication = validation['publication']
        self.assertTrue(publication['region']['container'])
        self.assertEqual(self.receipt()['region'], publication['region'])
        capture = read_structure(self.school)
        self.assertTrue(any(node.get('source_id') == self.source.id for node in graph_projection(capture,self.school)['nodes']))
        paths=source_paths(capture)[self.source.id]
        self.assertTrue(any(path['relation_type'] == 'source_on_page' for path in paths),paths)

    def test_newer_independent_page_and_executed_region_are_shared_by_all_readers(self):
        evidence = self.capture(changed_independent=True)
        first, independent = evidence.bundle['list'], evidence.bundle['independent']
        self.assertNotEqual(first['hash'], independent['hash'])
        evidence.bundle['region'] = dict(container='#unexecuted', heading='#unexecuted', name='通知公告')
        proposal, validation = self.save(evidence)
        receipt = self.receipt()
        self.assertEqual(receipt['list_observation']['hash'], independent['hash'])
        self.assertEqual(receipt['region'], validation['publication']['region'])
        self.assertNotEqual(receipt['region'], evidence.bundle['region'])
        capture = read_structure(self.school)
        page = next(page for page in capture['report']['pages'] if page['url'] == self.source.list_url)
        self.assertEqual(page['content_hash'], receipt['content_hash'])
        bindings = [edge for edge in capture['facts']['relations'] if edge['type']=='source_on_page'
            and edge['target']=='source:'+str(self.source.id)
            and any(a.get('kind')=='validated_publication_region' for a in edge.get('assertions',[]))]
        self.assertTrue(bindings)

    def test_invalid_derived_publication_cannot_fall_back_to_the_old_positive_region(self):
        proposal, validation = self.save(self.capture())
        self.assertEqual(self.receipt()['source_id'], self.source.id)
        changed = deepcopy(validation)
        changed['publication'] = dict(list_snapshot={'url':self.source.list_url,'hash':'a'*64},
            region=dict(container='#notices',heading='#notices h2'))
        proposal.validation_json = json.dumps(changed)
        db.session.commit()
        self.assertEqual(load_source_receipts(dict(name=self.school.name,root_url=self.school.url)), [])


if __name__ == '__main__':
    unittest.main()
