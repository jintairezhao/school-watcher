"""Scheduling credit comes from current executed receipts, never URL guesses."""
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy import event

from backend.database.db import db
from backend.database.models import Department, School
from backend.database.source_governance_models import SourceProposal, SourceValidation
from backend.services import source_governance as governance
from backend.services.source_path_receipts import load_source_receipts
from tests import test_direct_onboarding as native_fixture


class SourcePathReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.DirectOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.school = db.session.get(School, self.fixture.school_id)
        self.site = {'name': self.school.name, 'root_url': self.school.url}
        self.source = Department(school_id=self.school.id, name='通知公告', group_name='学生工作处',
            list_url=native_fixture.URL, list_selector='#notices li', title_selector='a',
            link_selector='a', date_selector='span', content_selector='article')
        db.session.add(self.source)
        db.session.commit()

    def detail(self, **changes):
        return dict({'status': 'full', 'list_snapshot': {
            'hash': governance._hash(native_fixture.HTML), 'url': self.source.list_url, 'outcome': 'usable'},
            'region': {'container': '#notices', 'heading': '#notices h2', 'name': '通知公告'},
            'articles': {'passed': True, 'matched_count': 3}}, **changes)

    def native(self, detail=None, *, source=None, config_hash=None, at=None, department_id='source'):
        source = source or self.source
        row = SourceValidation(school_id=source.school_id,
            department_id=source.id if department_id == 'source' else department_id,
            config_hash=config_hash or governance._hash(governance.source_config(source)),
            validation_json=governance._json(self.detail() if detail is None else detail),
            created_at=at or datetime.utcnow())
        db.session.add(row)
        db.session.commit()
        return row

    def public(self, *, source=None, state='activated', at=None):
        source = source or self.source
        config = governance.source_config(source)
        bundle = {'config': config, 'config_hash': governance._hash(config),
            'school_id': source.school_id, 'root_url': source.school.url,
            'list': self.detail()['list_snapshot'], 'region': self.detail()['region']}
        candidate, digest = governance._json(config), governance._hash(bundle)
        row = SourceProposal(school_id=source.school_id, department_id=source.id,
            identity_key='identity', proposal_key='proposal-' + str(SourceProposal.query.count()),
            origin='configuration', state=state, candidate_json=candidate,
            expected_config_hash=governance._hash(config), evidence_json=governance._json(bundle),
            evidence_hash=digest, validation_json=governance._json({'passed': True,
                'errors': [], 'item_count': 3}), validator_version=governance.VERSION,
            validated_hash=governance._hash([candidate, digest, governance.VERSION]),
            updated_at=at or datetime.utcnow())
        db.session.add(row)
        db.session.commit()
        return row

    def load(self):
        with patch.object(governance, 'read_snapshot', side_effect=AssertionError('No HTML read')), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No paid call')), \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=AssertionError('No network')):
            return load_source_receipts(self.site)

    def test_native_current_receipt_and_duplicate_validation_yield_one_stable_source(self):
        self.native()
        latest = self.native()
        first, second = self.load(), self.load()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['source_id'], self.source.id)
        self.assertEqual(first[0]['receipt_id'], 'native:' + str(latest.id))
        self.assertEqual(first[0]['content_hash'], governance._hash(native_fixture.HTML))
        self.assertEqual(first[0]['region']['container'], '#notices')

    def test_no_app_or_policy_only_flask_context_has_no_receipts(self):
        self.fixture.ctx.pop()
        try:
            self.assertEqual(load_source_receipts(self.site), [])
            with Flask(__name__).app_context():
                self.assertEqual(load_source_receipts(self.site), [])
        finally:
            self.fixture.ctx.push()

    def test_absent_failed_empty_and_unconfirmed_materials_are_neutral(self):
        self.assertEqual(self.load(), [])
        for detail in (self.detail(status='empty'), self.detail(status='rejected'),
                       self.detail(articles={'passed': False}), self.detail(articles={'passed': 1}),
                       self.detail(articles='{"passed":true}'),
                       self.detail(region={'container': '#notices'}), self.detail(region='invalid'),
                       self.detail(region={'container': '#notices', 'heading': 'h2', 'empty': True}),
                       self.detail(errors=['article_identity_conflict']),
                       self.detail(list_snapshot={'hash': 'bad', 'url': self.source.list_url}),
                       self.detail(list_snapshot={'hash': 'a' * 64, 'url': self.source.list_url,
                                                  'outcome': 'needs_adapter'})):
            with self.subTest(detail=detail):
                self.native(detail)
                self.assertEqual(self.load(), [])

    def test_limited_and_partial_body_validation_can_supply_one_source(self):
        for status in ('limited', 'partial'):
            with self.subTest(status=status):
                self.native(self.detail(status=status, articles={'passed': True, 'matched_count': 1}))
                self.assertEqual([row['source_id'] for row in self.load()], [self.source.id])

    def test_recognition_digest_does_not_replace_the_actual_html_hash(self):
        self.native(self.detail(page_hash='0' * 64))
        self.assertEqual(self.load()[0]['content_hash'], governance._hash(native_fixture.HTML))

    def test_wrong_source_url_school_and_configuration_cannot_borrow_receipts(self):
        self.native(self.detail(list_snapshot={'hash': 'a' * 64,
                                              'url': 'https://example.edu.cn/another/'}))
        self.assertEqual(self.load(), [])
        self.native(self.detail(school_id=self.school.id + 100))
        self.assertEqual(self.load(), [])
        self.native()
        self.source.list_selector = '#changed li'
        db.session.commit()
        self.assertEqual(self.load(), [])

    def test_new_failed_current_config_without_department_id_blocks_old_success(self):
        self.native()
        self.native(self.detail(status='material_limited'), department_id=None)
        self.assertEqual(self.load(), [])

    def test_new_bad_record_does_not_fall_back_to_old_positive(self):
        self.native()
        bad = self.native()
        for raw in ('{broken', '[]', 'null', '42'):
            with self.subTest(raw=raw):
                bad.validation_json = raw
                db.session.commit()
                self.assertEqual(self.load(), [])

    def test_complete_name_and_root_scope_select_unique_school(self):
        self.native()
        other = School(name='测试大学独立学院', url=self.school.url)
        db.session.add(other)
        db.session.commit()
        self.assertEqual(len(self.load()), 1)
        self.assertEqual(load_source_receipts(dict(self.site, name=other.name)), [])
        self.assertEqual(load_source_receipts(dict(self.site, root_url=self.school.url + 'campus/')), [])
        self.assertEqual(load_source_receipts(dict(self.site, root_url='https://sub.example.edu.cn/')), [])
        db.session.add(School(name=self.school.name, url=self.school.url))
        db.session.commit()
        self.assertEqual(self.load(), [])

    def test_same_page_distinct_real_columns_retain_separate_stable_ids(self):
        self.native()
        second = Department(school_id=self.school.id, **dict(governance.source_config(self.source),
            name='招生公告', list_selector='#admissions li'))
        db.session.add(second)
        db.session.commit()
        self.native(self.detail(region={'container': '#admissions', 'heading': '#admissions h2',
                                       'name': '招生公告'}), source=second)
        self.assertEqual({row['source_id'] for row in self.load()}, {self.source.id, second.id})

    def test_public_activated_receipt_and_repeated_native_path_deduplicate_by_source(self):
        public = self.public()
        self.assertEqual(self.load()[0]['receipt_id'], 'proposal:' + str(public.id))
        self.native()
        self.assertEqual(len(self.load()), 1)

    def test_public_draft_failed_validation_empty_or_tampered_hashes_do_not_reward(self):
        public = self.public(state='proposed')
        self.assertEqual(self.load(), [])
        public.state = 'activated'
        db.session.commit()
        self.assertEqual(len(self.load()), 1)
        changes = ({'validation_json': '{broken'},
                   {'validation_json': governance._json({'passed': False, 'item_count': 3})},
                   {'validation_json': governance._json({'passed': True, 'item_count': 0})},
                   {'validation_json': governance._json({'passed': True, 'item_count': True})},
                   {'evidence_hash': '0' * 64}, {'evidence_json': '[]'},
                   {'candidate_json': '{broken'}, {'validated_hash': '0' * 64},
                   {'validator_version': 'unknown-validator'})
        for values in changes:
            previous = {key: getattr(public, key) for key in values}
            for key, value in values.items():
                setattr(public, key, value)
            db.session.commit()
            with self.subTest(values=values):
                self.assertEqual(self.load(), [])
            for key, value in previous.items():
                setattr(public, key, value)
            db.session.commit()

    def test_latest_invalid_public_receipt_does_not_borrow_older_active_success(self):
        self.public()
        latest = self.public()
        latest.candidate_json = '{broken'
        db.session.commit()
        self.assertEqual(self.load(), [])

    def test_consistently_hashed_public_bundle_must_still_match_school_config_and_region(self):
        public = self.public()
        original = json.loads(public.evidence_json)
        changes = ({'root_url': 'https://example.edu.cn/other-campus/'},
                   {'school_id': self.school.id + 100}, {'config_hash': '0' * 64},
                   {'config': dict(original['config'], name='另一栏目')},
                   {'list': dict(original['list'], url=self.source.list_url + 'another/')},
                   {'region': {'container': '#notices'}},
                   {'region': dict(original['region'], empty=True)})
        for values in changes:
            bundle = dict(original, **values)
            public.evidence_json = governance._json(bundle)
            public.evidence_hash = governance._hash(bundle)
            public.validated_hash = governance._hash([
                public.candidate_json, public.evidence_hash, governance.VERSION])
            db.session.commit()
            with self.subTest(values=values):
                self.assertEqual(self.load(), [])

    def test_public_receipt_for_previous_live_config_does_not_reward_changed_rule(self):
        self.public()
        self.source.content_selector = '#new-article'
        db.session.commit()
        self.assertEqual(self.load(), [])

    def test_new_failure_from_other_writer_blocks_old_positive(self):
        self.public(at=datetime.utcnow() - timedelta(seconds=2))
        self.native(self.detail(status='rejected'))
        self.assertEqual(self.load(), [])
        self.public(at=datetime.utcnow() + timedelta(seconds=1))
        self.assertEqual(len(self.load()), 1)

    def test_read_does_not_flush_pending_changes_or_mutate_database(self):
        self.native()
        pending = School(name='not written', url='https://pending.example.edu.cn/')
        db.session.add(pending)
        statements = []
        def executed(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', executed)
        try:
            self.assertEqual(len(self.load()), 1)
        finally:
            event.remove(db.engine, 'before_cursor_execute', executed)
        self.assertIsNone(pending.id)
        self.assertFalse(any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE'))
                             for sql in statements))

    def test_database_errors_are_not_disguised_as_no_evidence(self):
        def unavailable(*args):
            raise RuntimeError('database unavailable')
        event.listen(db.engine, 'before_cursor_execute', unavailable)
        try:
            with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
                self.load()
        finally:
            event.remove(db.engine, 'before_cursor_execute', unavailable)


class ExecutedReceiptCompatibilityTests(unittest.TestCase):
    def test_native_actual_onboarding_receipt_is_consumed_without_new_io(self):
        fixture = native_fixture.DirectOnboardingTests()
        fixture.setUp()
        try:
            from backend.services.source_onboarding import onboard_page
            result = onboard_page({'school_id': fixture.school_id, 'url': native_fixture.URL,
                                   'ai_assist': False}, fetcher=fixture.fetch)
            self.assertEqual(result['state'], 'connected')
            school = db.session.get(School, fixture.school_id)
            with patch.object(governance, 'read_snapshot', side_effect=AssertionError('No read')):
                receipts = load_source_receipts({'name': school.name, 'root_url': school.url})
            self.assertEqual([row['source_id'] for row in receipts], result['department_ids'])
            self.assertEqual(receipts[0]['content_hash'], governance._hash(native_fixture.HTML))
        finally:
            fixture.tearDown()

    def test_public_actual_capture_validation_and_activation_receipt_is_consumed(self):
        from tests.test_source_governance import SourceGovernanceTests
        fixture = SourceGovernanceTests()
        fixture.setUp()
        try:
            proposal = fixture.proposal()
            self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
            source = governance.activate_proposal(proposal.id)
            with patch.object(governance, 'read_snapshot', side_effect=AssertionError('No read')):
                receipts = load_source_receipts({'name': fixture.school.name,
                                                'root_url': fixture.school.url})
            self.assertEqual([row['source_id'] for row in receipts], [source.id])
            self.assertEqual(receipts[0]['receipt_id'], 'proposal:' + str(proposal.id))
        finally:
            fixture.tearDown()
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
