"""A first-pass tie retains the existing, evidence-gated college preference."""
from copy import deepcopy
import unittest

from backend.scraper.discovery.attention import candidate_attention
from backend.scraper.discovery.frontier import choose_route


ROOT = 'https://example.edu.cn/'


class PrimaryEntranceTieTests(unittest.TestCase):
    def setUp(self):
        self.root = dict(url=ROOT, label='学校', kind='root', state='fetched',
                         status_code=200, content_hash='a' * 64)
        self.pages = [dict(url=ROOT + path, label=label, kind=kind, depth=1,
                           frontier_rowid=index, path_json='[]', state='pending')
                      for index, (path, label, kind) in enumerate([
                          ('continuing/admissions', '继续教育招生', 'channel'),
                          ('petroleum', '石油与天然气工程学院', 'unit'),
                          ('geoscience', '地球科学与技术学院', 'unit')], 1)]
        self.edges = [dict(parent_url=ROOT, target_url=p['url'], decision='follow',
                           content_hash='a' * 64, locator=f'#menu li:nth-child({i}) a')
                      for i, p in enumerate(self.pages, 1)]

    def choose(self, **changes):
        trace = []
        args = dict(edges=self.edges, parents={ROOT: self.root}, attention={},
                    history={ROOT: self.root, **{p['url']: p for p in self.pages}},
                    claim_count=0, explain=trace.append)
        args.update(changes)
        selected = choose_route(self.pages, **args)
        return selected, trace[0]

    def test_observed_colleges_win_equal_need_before_menu_insertion_order(self):
        self.assertEqual([candidate_attention(p)['score'] for p in self.pages], [1, 1, 1])
        selected, trace = self.choose()
        self.assertEqual(selected['url'], self.pages[1]['url'])
        self.assertEqual(trace['reason'], 'unread_school_trunk')
        self.assertEqual(trace['attention']['trunk_evidence'][0]['basis'], 'direct_school_need_entrance')

    def test_greater_direct_need_still_precedes_college_tie_break(self):
        self.pages[0]['label'] = '本科招生 就业 学生资助'
        self.assertGreater(candidate_attention(self.pages[0])['score'],
                           candidate_attention(self.pages[1])['score'])
        self.assertEqual(self.choose()[0]['url'], self.pages[0]['url'])

    def test_stale_material_cannot_create_a_primary_college_entrance(self):
        edges = deepcopy(self.edges)
        for edge in edges[1:]:
            edge['content_hash'] = 'b' * 64
        selected, trace = self.choose(edges=edges)
        self.assertEqual(selected['url'], self.pages[0]['url'])
        self.assertTrue(all(p['content_hash'] == self.root['content_hash']
                            for p in trace['attention']['trunk_evidence']))

    def test_oldest_pending_fairness_turn_remains_authoritative(self):
        selected, trace = self.choose(claim_count=4)
        self.assertEqual(selected['url'], self.pages[0]['url'])
        self.assertEqual(trace['reason'], 'oldest_pending_turn')
