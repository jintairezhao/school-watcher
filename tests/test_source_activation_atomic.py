"""Review activation publishes source configuration and existing structural proof together."""
import json
import sqlite3
import unittest
from unittest.mock import patch
from contextlib import closing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event

from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry, Announcement, UserRead, Subscription, User, BackgroundTask
from backend.database.source_governance_models import SourceConfigVersion, SourceProposal, SourceReviewEvent
from backend.services import source_governance as governance
from backend.services.structure_snapshot import read_structure, graph_projection, source_choices, transport_update
from backend.services.structure_versions import current_version
from tests import test_school_structure as fixture


class SourceActivationAtomicTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SchoolStructureTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.publish()
        self.school = self.f.school
        self.config = dict(name='通知公告', group_name='计算机学院', list_url=fixture.CS + 'notices/',
            list_selector='#notices li', title_selector='a', link_selector='a', date_selector='span', content_selector='article')

    def proposal(self, source=None):
        def fetch(url, purpose):
            return self.f.fixture.fetch(url, purpose) if purpose == 'body' else fixture.HTML
        evidence = governance.capture_source_evidence(self.school.id, self.config,
            department_id=source.id if source else None, seed_html=fixture.HTML,
            inventory=self.f.catalog, fetcher=fetch)
        proposal = governance.propose_source(self.school.id, self.config, evidence,
            department_id=source.id if source else None, origin='manual_review')
        result = governance.validate_proposal(proposal.id)
        self.assertTrue(result['passed'], result)
        return proposal

    def legacy_source(self):
        source = Department(school_id=self.school.id, name='旧栏目名', group_name='旧显示分组',
            list_url=self.config['list_url'], list_selector='#old li', title_selector='a', link_selector='a', content_selector='article')
        db.session.add(source); db.session.flush()
        article = Announcement(school_id=self.school.id, department_id=source.id, title='历史通知', url=fixture.CS + 'history.htm')
        db.session.add(article); db.session.flush()
        user = User.query.first()
        db.session.add(UserRead(user_id=user.id, announcement_id=article.id))
        subscription = Subscription.query.one()
        subscription.department_ids = [source.id]
        db.session.commit()
        return source

    def owners(self, ident):
        return [fact for row in DepartmentDirectoryEntry.query.filter_by(department_id=ident)
                for fact in row.facts_json if fact['type'] == 'source_ownership']

    def test_new_activation_has_owner_at_its_only_commit_and_same_view_version(self):
        proposal = self.proposal()
        before = current_version(self.school.id)
        capture = read_structure(self.school)
        prior = transport_update(graph_projection(capture, self.school), school_id=self.school.id)
        commits = []
        database = db.engine.url.database
        school_id = self.school.id
        def observed_commit(session):
            with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as reader:
                source = reader.execute("select id from departments where list_selector='#notices li'").fetchone()
                facts = [fact for row in reader.execute('select facts_json from department_directory_entries where department_id=?', source)
                         for fact in json.loads(row[0])]
                commits.append(dict(ownership=any(fact['type'] == 'source_ownership' for fact in facts),
                    revision=reader.execute('select revision from structure_revisions where school_id=?', (school_id,)).fetchone()[0]))
        session = db.session()
        event.listen(session, 'after_commit', observed_commit)
        try:
            source = governance.activate_proposal(proposal.id)
        finally:
            event.remove(session, 'after_commit', observed_commit)
        self.assertEqual(commits, [dict(ownership=True, revision=before['revision'] + 1)])
        self.assertTrue(self.owners(source.id))
        after = read_structure(self.school)
        graph = graph_projection(after, self.school)
        self.assertEqual(after['version']['revision'], before['revision'] + 1)
        self.assertEqual(after['version']['epoch'], before['epoch'])
        self.assertTrue(any(edge['type'] == 'source_ownership' and edge['target'] == f'source:{source.id}' for edge in graph['relations']))
        self.assertTrue(any(group['name'] == '计算机学院' and any(column['department'].id == source.id for column in group['columns'])
            for group in source_choices(after)['有依据的栏目归属']))
        update = transport_update(graph, school_id=self.school.id, since=prior['structure_token'])
        self.assertEqual(update['update_mode'], 'delta')
        for field in ('nodes', 'relations'):
            rebuilt = {row['id']: row for row in prior[field]}
            for ident in update['delta'][field]['remove']: rebuilt.pop(ident, None)
            rebuilt.update({row['id']: row for row in update['delta'][field]['upsert']})
            self.assertEqual(rebuilt, {row['id']: row for row in graph[field]})

    def test_existing_activation_preserves_personal_state_and_retry_does_not_add_versions(self):
        source = self.legacy_source(); ident = source.id
        proposal = self.proposal(source)
        before = current_version(self.school.id)
        self.assertEqual(governance.activate_proposal(proposal.id).id, ident)
        self.assertTrue(self.owners(ident))
        after = current_version(self.school.id)
        self.assertEqual(after['revision'], before['revision'] + 1)
        self.assertEqual(Subscription.query.one().department_ids, [ident])
        self.assertEqual(Announcement.query.one().title, '历史通知')
        self.assertEqual(UserRead.query.count(), 1)
        self.assertEqual(governance.activate_proposal(proposal.id).id, ident)
        self.assertEqual(SourceConfigVersion.query.count(), 1)
        self.assertEqual(current_version(self.school.id), after)

    def test_structural_failure_rolls_back_before_a_caller_can_commit_partial_activation(self):
        source = self.legacy_source(); ident = source.id
        expected = governance.source_config(source)
        proposal = self.proposal(source); proposal_id = proposal.id
        before = current_version(self.school.id)
        tasks = BackgroundTask.query.count()
        from backend.services.school_structure import sync_official_structure
        def fail_after_staging(*args, **kwargs):
            sync_official_structure(*args, **kwargs)
            raise RuntimeError('injected structural failure after staging')
        with patch('backend.services.school_structure.sync_official_structure', side_effect=fail_after_staging):
            with self.assertRaisesRegex(RuntimeError, 'injected structural failure'):
                governance.activate_proposal(proposal_id)
        db.session.commit()  # A caller catching the failure cannot publish partial state.
        self.assertEqual(governance.source_config(db.session.get(Department, ident)), expected)
        self.assertEqual(db.session.get(SourceProposal, proposal_id).state, 'validated')
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertEqual(BackgroundTask.query.count(), tasks)
        self.assertEqual(SourceReviewEvent.query.filter_by(action='activate').count(), 0)
        self.assertFalse(self.owners(ident))
        self.assertEqual(current_version(self.school.id), before)
        self.assertEqual(Subscription.query.one().department_ids, [ident])
        self.assertEqual(UserRead.query.count(), 1)
        self.assertEqual(governance.activate_proposal(proposal_id).id, ident)
        self.assertTrue(self.owners(ident))
        self.assertEqual(current_version(self.school.id)['revision'], before['revision'] + 1)

    def test_lease_loss_after_structure_staging_rolls_back_every_change(self):
        from backend.services.tasks import LeaseLost
        from backend.services.school_structure import sync_official_structure
        source = self.legacy_source(); ident = source.id
        expected = governance.source_config(source)
        proposal = self.proposal(source)
        before = current_version(self.school.id)
        staged = []
        def stage(*args, **kwargs):
            result = sync_official_structure(*args, **kwargs)
            staged.append(True)
            return result
        def fence():
            if staged: raise LeaseLost()
        with patch('backend.services.school_structure.sync_official_structure', side_effect=stage), \
                patch('backend.services.tasks.assert_owned', side_effect=fence):
            with self.assertRaises(LeaseLost): governance.activate_proposal(proposal.id)
        db.session.commit()
        self.assertTrue(staged)
        self.assertEqual(governance.source_config(db.session.get(Department, ident)), expected)
        self.assertFalse(self.owners(ident))
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertEqual(current_version(self.school.id), before)

    def test_catalogue_cannot_advance_while_activation_uses_its_pinned_material(self):
        from filelock import Timeout
        from backend.services.runtime_catalog import catalog_publication_lock
        from backend.services.school_structure import sync_official_structure
        proposal = self.proposal()
        root = self.f.fixture.app.config['SOURCE_CATALOG_PATH']
        observed = []
        def competing_publisher_lock():
            try:
                with catalog_publication_lock(root, timeout=0): return 'acquired'
            except Timeout: return 'blocked'
        def check_then_stage(*args, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                observed.append(pool.submit(competing_publisher_lock).result(timeout=2))
            return sync_official_structure(*args, **kwargs)
        with patch('backend.services.school_structure.sync_official_structure', side_effect=check_then_stage):
            source = governance.activate_proposal(proposal.id)
        self.assertEqual(observed, ['blocked'])
        self.assertTrue(self.owners(source.id))


if __name__ == '__main__': unittest.main()
