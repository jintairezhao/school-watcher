"""Retained metadata cannot replace missing original structural evidence."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.services.structure_facts import build_structure_facts
from backend.services.structure_snapshot import read_structure, graph_projection
from tests import test_school_structure as fixture
from tests import test_structure_facts as facts_fixture

ROOT, DIRECTORY = facts_fixture.ROOT, facts_fixture.DIRECTORY


class MissingOriginalFactsTests(unittest.TestCase):
    def setUp(self):
        self.f = facts_fixture.StructureFactsTests()
        self.f.setUp()
        self.data = self.f.data
        self.data['navigation_edges'] = [dict(parent_url=ROOT, target_url=DIRECTORY,
            **self.f.proof(ROOT, 'nav a'))]

    def test_explicit_missing_original_cannot_establish_a_navigation_fact(self):
        self.data['pages'][0]['material_available'] = False
        facts = build_structure_facts(**self.data)
        self.assertFalse(any(r['type'] == 'navigation_link' for r in facts['relations']))
        self.assertIn(dict(code='original_material_not_retained', reference_url=ROOT,
            content_hash=self.data['pages'][0]['content_hash']), facts['gaps'])
        self.assertEqual(len(self.data['navigation_edges']), 1)

    def test_missing_web_original_does_not_remove_saved_source_configuration(self):
        self.data['departments'] = [self.f.source(1)]
        for page in self.data['pages']:
            page['material_available'] = False
        facts = build_structure_facts(**self.data)
        sources = [n for n in facts['nodes'] if n['entity_type'] == 'source']
        self.assertEqual([n['id'] for n in sources], ['source:1'])
        self.assertEqual([r['type'] for r in facts['relations']], ['source_on_page'])
        self.assertEqual(facts['relations'][0]['evidence'][0]['kind'], 'source_configuration')

    def test_current_page_availability_overrides_an_old_record_flag(self):
        self.data['pages'][0]['material_available'] = True
        self.data['navigation_edges'][0]['material_available'] = False
        facts = build_structure_facts(**self.data)
        navigation, = facts['relations']
        self.assertIs(navigation['evidence'][0]['material_available'], True)
        # A stale positive flag cannot rescue an unavailable current page.
        self.data['pages'][0]['material_available'] = False
        self.data['navigation_edges'][0]['material_available'] = True
        self.assertFalse(build_structure_facts(**self.data)['relations'])


class PublishedOriginalAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SchoolStructureTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.model = patch('backend.ai.providers.complete', side_effect=AssertionError('No model'))
        self.model.start()
        self.addCleanup(self.model.stop)
        self.f.publish()

    def forget_original(self, url):
        # Reproduce the legacy catalogue state; metadata and original parser
        # observations survive, but this disposable fixture has no body.
        pinned = self.f.catalog.pin(self.f.key)
        with pinned.connect(write=True) as c:
            c.execute('UPDATE catalog_pages SET evidence_gzip=NULL WHERE url=?', (url,))
        return pinned

    def test_readers_report_actual_retained_body_without_fetching_it(self):
        address = fixture.ROOT + 'colleges/'
        pinned = self.forget_original(address)
        with patch.object(pinned, 'snapshot', side_effect=AssertionError('No HTML loading')):
            self.assertIs(pinned.get_page(self.f.key, address)['material_available'], False)
            report = pinned.report(self.f.key)
            self.assertIs(next(p for p in report['pages'] if p['url'] == address)['material_available'], False)
            monitor = pinned.monitor_snapshot(self.f.key)
            self.assertIs(next(p for p in monitor['pages'] if p['url'] == address)['material_available'], False)
            records = [r for r in pinned.structure(self.f.key) if r['reference_url'] == address]
            self.assertTrue(records)
            self.assertTrue(all(r['material_available'] is False for r in records))
            self.assertIs(pinned.get_page(self.f.key, fixture.CS)['material_available'], True)

    def test_missing_original_leaves_history_but_no_confirmed_path_until_republication(self):
        before = read_structure(self.f.school)
        self.assertTrue(any(r['type'] == 'navigation_link' for r in before['facts']['relations']))
        pinned = self.forget_original(fixture.ROOT)
        history = deepcopy(pinned.monitor_snapshot(self.f.key)['navigation_edges'])
        self.assertTrue(any(e['parent_url'] == fixture.ROOT for e in history))
        after = read_structure(self.f.school, include_catalog_report=True)
        for scope in ('connected', 'exploration'):
            graph = graph_projection(after, self.f.school, scope=scope)
            nodes = {n['id']: n for n in graph['nodes']}
            self.assertFalse(any(e['type'] == 'navigation_link'
                and nodes[e['source']]['url'] == fixture.ROOT for e in graph['relations']))
        self.assertTrue(any(g['code'] == 'original_material_not_retained' for g in after['facts']['gaps']))
        self.assertEqual(pinned.monitor_snapshot(self.f.key)['navigation_edges'], history)
        # Restore through normal publication using the existing exact original,
        # with no website/model call or manufactured observation timestamp.
        self.f.catalog.publish(self.f.inventory, self.f.key, merge=True)
        restored = read_structure(self.f.school)
        nodes = {n['id']: n for n in restored['facts']['nodes']}
        self.assertTrue(any(e['type'] == 'navigation_link'
            and nodes[e['source']]['url'] == fixture.ROOT for e in restored['facts']['relations']))
        self.assertIs(self.f.catalog.pin(self.f.key).get_page(self.f.key, fixture.ROOT)['material_available'], True)
        self.assertIs(pinned.get_page(self.f.key, fixture.ROOT)['material_available'], False)


if __name__ == '__main__':
    unittest.main()
