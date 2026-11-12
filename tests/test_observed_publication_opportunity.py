"""Already observed publication previews are usable outside faculty homepages."""
from copy import deepcopy
import json
import unittest

from backend.scraper.discovery.frontier import choose_route


ROOT = 'https://example.edu.cn/'
HOME = 'https://admissions.example.edu.cn/'
TARGET = HOME + 'masters/notices.htm'
DIGEST = 'a' * 64


class ObservedPublicationOpportunityTests(unittest.TestCase):
    def setUp(self):
        self.column = dict(name='硕士通知', column_url=TARGET,
            container_locator='#masters ul', heading_locator='#masters h2',
            column_link_locator='#masters h2 a',
            samples=[dict(url=HOME + 'article/1.htm', title='2027年硕士招生报名说明')])
        self.parent = dict(url=HOME, kind='channel', label='招生就业',
            state='fetched', status_code=200, content_hash=DIGEST,
            feed_json={'lists': [self.column]})
        self.root = dict(url=ROOT, kind='root', state='fetched', status_code=200,
                         content_hash='b' * 64)
        self.other = dict(url=ROOT + 'college/', label='工程学院', kind='unit',
                          state='pending', depth=1, frontier_rowid=1, path_json='[]')
        self.notice = dict(url=TARGET, label='硕士通知', kind='channel',
            state='pending', depth=2, frontier_rowid=2, path_json='["招生就业"]')
        self.edge = dict(parent_url=HOME, target_url=TARGET, decision='follow',
            content_hash=DIGEST, locator='#masters h2 a')

    def choose(self, *, parent=None, edge=None, notice=None, attention=None, claim_count=0):
        parent = self.parent if parent is None else parent
        notice = self.notice if notice is None else notice
        args = dict(pages=[self.other, notice], parents={ROOT:self.root, HOME:parent},
            edges=[dict(parent_url=ROOT, target_url=self.other['url'], decision='follow',
                        content_hash='b' * 64, locator='#colleges a'),
                   self.edge if edge is None else edge],
            history={ROOT:self.root, HOME:parent, self.other['url']:self.other, TARGET:notice},
            attention={} if attention is None else attention, claim_count=claim_count)
        before = deepcopy(args)
        trace = []
        chosen = choose_route(**args, explain=trace.append)
        self.assertEqual(args, before)
        return chosen, trace[0]

    def assert_no_preview_turn(self, **kwargs):
        chosen, trace = self.choose(**kwargs)
        self.assertEqual(chosen['url'], self.other['url'])
        self.assertNotEqual(trace['reason'], 'unread_observed_publication')

    def test_observed_preview_on_topic_page_precedes_another_unread_unit(self):
        chosen, trace = self.choose()
        self.assertEqual(chosen['url'], TARGET)
        self.assertEqual(trace['reason'], 'unread_observed_publication')
        proof = trace['attention']['trunk_evidence'][0]
        self.assertEqual(proof['basis'], 'observed_publication_preview')
        self.assertEqual(proof['reference_url'], HOME)
        self.assertEqual(proof['content_hash'], DIGEST)
        self.assertEqual(proof['entry_locator'], self.edge['locator'])
        self.assertEqual(proof['audience_categories'], ['admissions'])
        self.assertEqual(self.parent['kind'], 'channel')

    def test_english_heading_uses_existing_audience_evidence(self):
        self.column.update(name='Admissions notices', samples=[dict(
            url=HOME+'article/1.htm', title='Applications for masters admissions')])
        self.notice['label'] = 'Admissions notices'
        self.assertEqual(self.choose()[0]['url'], TARGET)

    def test_generic_name_without_a_real_publication_preview_is_not_enough(self):
        for samples in ([], [dict(title='招生公告')], [dict(url='not-an-address', title='招生公告')]):
            with self.subTest(samples=samples):
                column = dict(self.column, samples=samples)
                self.assert_no_preview_turn(parent=dict(self.parent, feed_json={'lists':[column]}))

    def test_stale_hash_wrong_locator_and_non_followed_edge_revoke_the_turn(self):
        for change in ({'content_hash':'c'*64}, {'locator':'#different-menu'}, {'decision':'ignore'}):
            with self.subTest(change=change):
                self.assert_no_preview_turn(edge=dict(self.edge, **change))

    def test_incomplete_or_failed_parent_material_is_not_evidence(self):
        for change in ({'state':'pending'}, {'health':'needs_adapter'}, {'status_code':403},
                       {'content_hash':'invalid'}, {'error':'reading_failed'}):
            with self.subTest(change=change):
                self.assert_no_preview_turn(parent=dict(self.parent, **change))

    def test_unlocated_or_ambiguous_preview_is_not_a_named_entry(self):
        for change in ({'heading_locator':''}, {'column_link_locator':''},
                       {'container_locator':''}, {'name':''}, {'heading_ambiguous':True},
                       {'column_group_name':'招生就业'}):
            with self.subTest(change=change):
                column = dict(self.column, **change)
                self.assert_no_preview_turn(parent=dict(self.parent, feed_json={'lists':[column]}))

    def test_wide_menu_and_sibling_publications_cannot_lend_their_interest(self):
        unrelated = dict(self.column, name='图集', samples=[dict(
            url=HOME+'article/1.htm', title='春日校园')])
        sibling = dict(self.column, column_url=HOME+'doctors/notices.htm',
                       column_link_locator='#doctors h2 a')
        parent = dict(self.parent, feed_json={'lists':[unrelated, sibling]})
        self.assert_no_preview_turn(parent=parent)

    def test_an_inactive_audience_category_does_not_gain_priority(self):
        self.assert_no_preview_turn(attention={'profiles':['teacher']})

    def test_a_list_item_address_cannot_masquerade_as_the_list_entrance(self):
        self.column['samples'][0]['url'] = TARGET
        self.assert_no_preview_turn()

    def test_real_target_material_ends_the_initial_preference(self):
        self.assert_no_preview_turn(notice=dict(self.notice, content_hash='d'*64))

    def test_failed_transport_alone_does_not_consume_the_material_opportunity(self):
        notice = dict(self.notice, state='failed', network_failures=1, error='network_unreachable')
        self.assertEqual(self.choose(notice=notice)[0]['url'], TARGET)

    def test_the_existing_oldest_pending_turn_is_preserved(self):
        chosen, trace = self.choose(claim_count=4)
        self.assertEqual(chosen['url'], self.other['url'])
        self.assertEqual(trace['reason'], 'oldest_pending_turn')

    def test_saved_string_feed_and_redirected_reference_keep_material_identity(self):
        parent = dict(self.parent, final_url=HOME+'index.htm',
                      feed_json=json.dumps(self.parent['feed_json']))
        chosen, trace = self.choose(parent=parent)
        self.assertEqual(chosen['url'], TARGET)
        self.assertEqual(trace['attention']['trunk_evidence'][0]['reference_url'], HOME+'index.htm')


if __name__ == '__main__':
    unittest.main()
