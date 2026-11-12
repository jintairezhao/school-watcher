"""Count evidenced same-page columns without merging their configurations."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from backend.database.db import db
from backend.database.models import Department, Subscription
from backend.services import source_governance as governance
from backend.services.source_path_receipts import load_source_receipts
from backend.scraper.discovery.foraging import ForagingFeedback
from backend.scraper.discovery.attention import coverage_state
from tests import test_source_path_receipts as receipt_fixture
from tests.test_direct_onboarding import HTML, TITLES


class SourceColumnObservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = receipt_fixture.SourcePathReceiptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source, self.site = self.fixture.source, self.fixture.site
        self.fixture.native()

    def duplicate(self, **changes):
        config = dict(governance.source_config(self.source), list_selector='section#notices > ul > li')
        config.update(changes)
        row = Department(school_id=self.source.school_id, **config)
        db.session.add(row)
        db.session.commit()
        self.fixture.native(source=row)
        return row

    def load(self, html=HTML):
        reads = []
        def material(url):
            reads.append(url)
            return html
        with patch('socket.getaddrinfo', side_effect=AssertionError('No network')):
            receipts = load_source_receipts(self.site, material=material)
        return receipts, reads

    def feedback(self, receipts):
        page = dict(url=self.source.list_url, label='本科生通知公告', kind='channel', depth=1,
            state='fetched', status_code=200, health='healthy', content_hash=governance._hash(HTML),
            feed_json={'name':'本科生通知公告'}, path_json='[]')
        feedback = ForagingFeedback({page['url']:page}, receipts=receipts)
        return feedback.features(page), page

    def test_equivalent_complete_dom_counts_once_and_preserves_both_receipts_and_subscription(self):
        other = self.duplicate()
        subscription = Subscription.query.one()
        subscription.department_ids = [self.source.id, other.id]
        db.session.commit()
        receipts, reads = self.load()
        self.assertEqual(reads, [self.source.list_url])
        self.assertEqual({r['source_id'] for r in receipts}, {self.source.id, other.id})
        self.assertEqual(len({r['column_observation']['fingerprint'] for r in receipts}), 1)
        info, _ = self.feedback(receipts)
        self.assertEqual(info['verified_source_count'], 1)
        self.assertEqual(info['verified_configuration_count'], 2)
        self.assertEqual(Subscription.query.one().department_ids, [self.source.id, other.id])
        self.assertEqual(Department.query.count(), 2)

    def test_coverage_counts_one_column_and_keeps_both_configuration_records(self):
        self.source.name = '奖学金通知公告'
        db.session.commit()
        html = HTML.replace('通知公告', '奖学金通知公告')
        detail = self.fixture.detail(list_snapshot={'url':self.source.list_url,'hash':governance._hash(html)},
            region={'container':'#notices','heading':'#notices h2','name':self.source.name})
        self.fixture.native(detail)
        other = self.duplicate()
        self.fixture.native(detail, source=other)
        receipts, _ = self.load(html)
        page = dict(url=self.source.list_url, kind='channel', label=self.source.name,
            content_hash=governance._hash(html), state='fetched', status_code=200, health='healthy')
        cells = coverage_state({page['url']:page}, branches={page['url']:page['url']}, validated_sources=receipts)['cells']
        self.assertTrue(cells)
        validated = [cell for categories in cells.values() for cell in categories.values() if cell['validated']]
        self.assertTrue(validated)
        for cell in validated:
            self.assertEqual(len(cell['validated']),1)
            self.assertEqual(len(cell['validation_records']),2)

    def test_separate_widgets_with_same_name_and_articles_stay_separate(self):
        html = HTML.replace('</body>', HTML.split('<body>')[1].split('</body>')[0].replace('id="notices"','id="other"')+'</body>')
        detail = self.fixture.detail(list_snapshot={'url':self.source.list_url,'hash':governance._hash(html)})
        self.fixture.native(detail)
        other = self.duplicate(list_selector='#other li')
        self.fixture.native(dict(detail,region={'container':'#other','heading':'#other h2','name':'通知公告'}),source=other)
        receipts,_ = self.load(html)
        self.assertEqual(len({r['column_observation']['fingerprint'] for r in receipts}),2)

    def test_inventory_claim_and_diagnostics_use_local_selection_observations(self):
        from pathlib import Path
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.frontier import choose_route
        self.duplicate()
        store = Inventory(Path(self.fixture.fixture.temp.name)/'column-inventory.sqlite3')
        key = store.ensure_site(self.site['name'],self.site['root_url'])
        store.finish(key,self.site['root_url'],html='<title>测试大学</title>',state='fetched',status_code=200)
        store.enqueue(key,self.source.list_url,'通知公告','channel',1,[],'school_domain')
        store.finish(key,self.source.list_url,html=HTML,state='fetched',status_code=200)
        store.enqueue(key,self.site['root_url']+'next','下一条通知','channel',1,[],'school_domain')
        with patch('backend.scraper.discovery.frontier.choose_route',wraps=choose_route) as choosing:
            self.assertIsNotNone(store.claim(key,focus='valuable'))
        receipts = choosing.call_args.kwargs['receipts']
        self.assertEqual(len(receipts),2)
        self.assertEqual(len({r['column_observation']['fingerprint'] for r in receipts}),1)
        with patch('backend.scraper.discovery.attention.coverage_state',wraps=coverage_state) as diagnosing:
            self.assertIsNotNone(store.exploration_diagnostics(key))
        self.assertTrue(all(r.get('column_observation') for r in diagnosing.call_args.kwargs['validated_sources']))

    def test_distinct_title_link_nodes_in_same_rows_never_share_observation(self):
        html = HTML
        for index, title in enumerate(TITLES):
            html = html.replace(f'{title}</a>', f'{title}</a><a href="/info/{index}.htm">{title}</a>')
        # Both exact configurations have their own current successful receipt.
        self.source.title_selector = self.source.link_selector = 'a:first-of-type'
        db.session.commit()
        detail = self.fixture.detail(list_snapshot={'hash':governance._hash(html), 'url':self.source.list_url})
        self.fixture.native(detail)
        self.duplicate(title_selector='a:last-of-type', link_selector='a:last-of-type')
        other = Department.query.filter(Department.id != self.source.id).one()
        self.fixture.native(detail, source=other)
        receipts, _ = self.load(html)
        self.assertEqual(len({r['column_observation']['fingerprint'] for r in receipts}), 2)

    def test_missing_or_changed_raw_retains_ids_without_equivalence_claim(self):
        other = self.duplicate()
        for raw in ('', HTML.replace('通知公告', '招生通知')):
            with self.subTest(raw_missing=not raw):
                receipts, _ = self.load(raw)
                self.assertEqual({r['source_id'] for r in receipts}, {self.source.id, other.id})
                self.assertFalse(any(r.get('column_observation') for r in receipts))
                info, _ = self.feedback(receipts)
                self.assertEqual(info['verified_source_count'], 2)
                self.assertEqual(info['same_page_identity_status'], 'incomplete')

    def test_matching_first_samples_do_not_hide_a_different_full_selection(self):
        self.duplicate(list_selector='#notices li:nth-child(-n+2)')
        receipts, _ = self.load()
        self.assertEqual(len({r['column_observation']['fingerprint'] for r in receipts}),2)
        info, _ = self.feedback(receipts)
        self.assertEqual(info['verified_source_count'],2)

    def test_different_publisher_does_not_share_count_identity(self):
        self.duplicate(group_name='另一个学院')
        receipts, _ = self.load()
        self.assertEqual(len({r['column_observation']['fingerprint'] for r in receipts}), 2)

    def test_stale_annotation_cannot_collapse_current_receipts(self):
        self.duplicate()
        receipts, _ = self.load()
        changed = deepcopy(receipts)
        for row in changed:
            row['column_observation']['config_hash'] = '0' * 64
        info, _ = self.feedback(changed)
        self.assertEqual(info['verified_source_count'], 2)
        self.assertEqual(info['same_page_identity_status'], 'incomplete')


if __name__ == '__main__':
    unittest.main()
