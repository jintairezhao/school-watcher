"""External handler evidence binds panels without changing a joint subscription."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.scraper.discovery.publication_lists import publication_lists

ROOT = Path(__file__).parent / 'fixtures/structure/student-tabs-20261025'
MATERIALS = json.loads((ROOT / 'materials.json').read_text('utf8'))
PAGE, SCRIPT = [gzip.decompress((ROOT / r['body_file']).read_bytes()).decode('utf8') for r in MATERIALS]
URL = MATERIALS[0]['url']
NAMES = ['通知公告', '综合新闻', '困难认定', '助学贷款', '本科生奖助',
         '研究生奖助', '勤工助学', '补偿代偿', '资助政策', '下载中心']


def panel_feeds(html):
    from bs4 import BeautifulSoup
    from backend.scraper.discovery.structure import locator
    order = {locator(p): i for i, p in enumerate(BeautifulSoup(html, 'lxml').select(
        '.c-zizhu-notice-module-detail__tab-div'))}
    return sorted((f for f in publication_lists(html, URL)
                   if f.get('tab_panel_locator') in order), key=lambda f: order[f['tab_panel_locator']])


class RetainedHTML(str):
    def __new__(cls, text=PAGE, scripts=None):
        value = super().__new__(cls, text)
        value.final_url = URL
        value.script_materials = deepcopy(scripts) if scripts is not None else [dict(
            url=MATERIALS[1]['url'], text=SCRIPT, hash=hashlib.sha256(SCRIPT.encode()).hexdigest(),
            page_url=URL, page_hash=hashlib.sha256(text.encode()).hexdigest(),
            observed_at=MATERIALS[1]['observed_at'], status=200)]
        return value


class StudentPublicationTabTests(unittest.TestCase):
    def test_old_review_reuses_newer_complete_observation_without_hiding_new_gaps(self):
        from tests.test_source_governance import SourceGovernanceTests
        from backend.database.db import db
        from backend.services import source_governance as g
        from backend.services.source_scope import configured_joint_scope, hold_configured_joint_scope
        fixture = SourceGovernanceTests();fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.dept.list_url = URL
        fixture.dept.list_selector = 'li.c-zizhu-module__half-li'
        fixture.dept.title_selector = fixture.dept.link_selector = 'a'
        db.session.commit()
        config = g.source_config(fixture.dept)

        def proposal(html, observed_at):
            reference = g._snapshot(html, URL)
            reference['observed_at'] = observed_at
            bundle = dict(school_id=fixture.school.id, config_hash=g._hash(config), config=config,
                          expected_config=config, list=reference)
            return g.propose_source(fixture.school.id, config, g.CapturedEvidence(bundle),
                                    department_id=fixture.dept.id)

        old = proposal(PAGE, '2026-10-23T12:40:12+00:00')
        self.assertTrue(hold_configured_joint_scope(old))
        complete = proposal(RetainedHTML(), '2026-10-25T06:50:46+00:00')
        self.assertTrue(hold_configured_joint_scope(complete))
        # An older pending record must see the newer observation, including its
        # true capture time, rather than discard assets at the same page hash.
        replayed = configured_joint_scope(old)
        self.assertEqual({r['name'] for r in replayed['regions']}, set(NAMES))
        self.assertEqual(replayed['material']['observed_at'], '2026-10-25T06:50:46+00:00')
        self.assertEqual(replayed['selected_items'], 55)
        # A later local retention timestamp is not a new website observation.
        retained_late = proposal(PAGE, None)
        retained_scope = configured_joint_scope(retained_late)
        self.assertEqual({r['name'] for r in retained_scope['regions']}, set(NAMES))
        self.assertEqual(retained_scope['material']['observed_at'], '2026-10-25T06:50:46+00:00')
        # A genuinely newer material gap must remain visible. Equal page HTML
        # cannot establish that the old external handler stayed unchanged.
        newer = proposal(PAGE, '2026-10-26T06:50:46+00:00')
        self.assertTrue(all(r['name_status'] == 'unknown' for r in configured_joint_scope(newer)['regions']))
        changed = RetainedHTML().script_materials
        changed_text = SCRIPT.replace('.eq(curIndex)', '.eq(curIndex+1)')
        self.assertNotEqual(changed_text, SCRIPT)
        changed[0].update(text=changed_text, hash=hashlib.sha256(changed_text.encode()).hexdigest(),
                          observed_at='2026-10-27T06:50:47+00:00')
        different_handler = proposal(RetainedHTML(scripts=changed), '2026-10-27T06:50:46+00:00')
        self.assertTrue(all(r['name_status'] == 'unknown'
                            for r in configured_joint_scope(different_handler)['regions']))

    def test_ten_panels_bind_to_real_controls_and_individual_entrances(self):
        feeds = panel_feeds(RetainedHTML())
        self.assertEqual([f['name'] for f in feeds], NAMES)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PAGE, 'lxml')
        self.assertEqual([len(soup.select_one(f['tab_panel_locator']).select('li')) for f in feeds], [6]*8 + [3, 4])
        self.assertEqual(len({f['column_url'] for f in feeds}), 10)
        for f in feeds:
            proof = f['tab_binding_evidence']
            self.assertEqual(proof['script_url'], MATERIALS[1]['url'])
            self.assertEqual(proof['script_hash'], hashlib.sha256(SCRIPT.encode()).hexdigest())
            self.assertEqual(proof['page_hash'], hashlib.sha256(PAGE.encode()).hexdigest())
            self.assertTrue(proof['script_locator'])
            self.assertTrue(f['heading_locator'] and f['tab_panel_locator'])

    def test_missing_or_unbound_script_is_unknown_instead_of_positional_guess(self):
        for key, bad in [('hash', '0'*64), ('page_hash', '0'*64),
                         ('page_url', URL+'else/'), ('url', URL+'js/missing.js'),
                         ('status', 404), ('observed_at', '')]:
            scripts = RetainedHTML().script_materials
            scripts[0][key] = bad
            with self.subTest(key=key):
                feeds = panel_feeds(RetainedHTML(scripts=scripts))
                self.assertEqual(len(feeds), 10)
                self.assertTrue(all(not f['name'] and not f['column_url'] for f in feeds))
        self.assertTrue(all(not f['name'] for f in panel_feeds(PAGE)))

    def test_changed_commented_quoted_or_duplicate_handler_cannot_prove_binding(self):
        for text in [SCRIPT.replace('.eq( curIndex )', '.eq( curIndex+1 )'),
                     '\n'.join('//'+line for line in SCRIPT.splitlines()), json.dumps(SCRIPT), SCRIPT+SCRIPT]:
            scripts = RetainedHTML().script_materials
            scripts[0].update(text=text, hash=hashlib.sha256(text.encode()).hexdigest())
            with self.subTest(text=text[:30]):
                self.assertTrue(all(not f['name'] and not f['column_url']
                    for f in panel_feeds(RetainedHTML(scripts=scripts))))

    def test_broken_local_panel_collection_cannot_borrow_neighbor_control(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(PAGE, 'lxml')
        soup.select('.c-zizhu-notice-module-detail__tab-ul > li')[0].decompose()
        feeds = panel_feeds(RetainedHTML(str(soup)))
        self.assertEqual([f['name'] for f in feeds[:2]], ['', ''])
        self.assertEqual([f['name'] for f in feeds[2:]], NAMES[2:])

    def test_snapshot_roundtrip_retains_script_proof_and_joint_scope(self):
        from tests.test_source_governance import SourceGovernanceTests
        from backend.database.db import db
        from backend.database.models import Subscription
        from backend.services import source_governance as g
        from backend.services.source_scope import observe_joint_selection
        fixture = SourceGovernanceTests();fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.dept.list_url = URL
        fixture.dept.list_selector = 'li.c-zizhu-module__half-li'
        fixture.dept.title_selector = 'a'
        fixture.dept.link_selector = 'a'
        db.session.add(Subscription(school_id=fixture.school.id, user_id=fixture.user.id,
                                    department_ids=[fixture.dept.id]))
        db.session.commit()
        config = g.source_config(fixture.dept)
        with patch('socket.socket.connect', side_effect=AssertionError('No network')):
            ref = g._snapshot(RetainedHTML(), URL)
            restored = g.read_snapshot(ref)
            self.assertEqual(str(restored), PAGE)
            self.assertEqual(g._hash(restored), ref['hash'])
            scope = observe_joint_selection(config, ref, restored)
            self.assertEqual(scope['selected_items'], 55)
            self.assertEqual({r['name'] for r in scope['regions']}, set(NAMES))
            self.assertTrue(all(r['tab_binding_evidence']['script_hash'] for r in scope['regions']))
            self.assertFalse(scope['publisher_assertion'])
            self.assertEqual(Subscription.query.one().department_ids, [fixture.dept.id])
            self.assertEqual(g.source_config(fixture.dept), config)
            script_ref = ref['script_materials'][0]
            # The existing evidence collector recursively retains script references
            # on a real pending proposal; clearing caches must not orphan proof.
            import os
            import time
            proposal = g.propose_source(fixture.school.id, config, g.CapturedEvidence(dict(
                school_id=fixture.school.id, config_hash=g._hash(config), config=config,
                expected_config=config, list=ref)), department_id=fixture.dept.id)
            from backend.services.source_scope import hold_configured_joint_scope
            self.assertTrue(hold_configured_joint_scope(proposal))
            script_path = g._evidence_root() / (script_ref['hash']+'.html.gz')
            old = time.time() - 40*86400
            os.utime(script_path, (old, old))
            g.prune_governance_evidence(all_cache=True)
            self.assertTrue(script_path.exists())
            (g._evidence_root() / (script_ref['hash']+'.html.gz')).unlink()
            degraded = observe_joint_selection(config, ref, g.read_snapshot(ref))
            self.assertEqual(degraded['selected_items'], 55)
            self.assertTrue(all(r['name_status'] == 'unknown' for r in degraded['regions']))


if __name__ == '__main__':
    unittest.main()
