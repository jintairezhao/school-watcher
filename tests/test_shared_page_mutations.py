"""Native same-page columns remain independent across committed mutations."""
from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup

from sqlalchemy.exc import OperationalError

from backend.database.db import db
from backend.database.models import (Announcement, AnnouncementSource, Department, DepartmentDirectoryEntry,
    Subscription, User, UserRead, UserAnnouncementState)
from backend.database.source_governance_models import SourceConfigVersion, SourceValidation
from backend.services import source_onboarding as onboarding
from backend.services.structure_snapshot import read_structure, source_choices, source_paths
from backend.services.structure_versions import current_version
from tests import test_school_structure as fixture
from tests import test_direct_onboarding as direct


class SharedPageMutationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SchoolStructureTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.guards = ExitStack(); self.addCleanup(self.guards.close)
        self.guards.enter_context(patch('socket.socket.connect', side_effect=AssertionError('no website')))
        self.guards.enter_context(patch('backend.ai.runtime.run_skill', side_effect=AssertionError('no model')))
        self.url = fixture.CS + 'notices/'
        lists = []
        for ident, title in [('ug', '本科生通知'), ('pg', '研究生通知')]:
            items = ''.join(f'<li><a href="/info/{i}.htm">{name}</a><span>2026-10-01</span></li>'
                for i, name in enumerate(direct.TITLES))
            lists.append(f'<section id="{ident}"><h2>{title}</h2><ul>{items}</ul></section>')
        self.f.pages[self.url] = '<html><title>测试大学计算机学院</title><body>' + ''.join(lists) + '</body></html>'
        self.f.publish()
        onboarding.queue_columns(self.f.school.id, self.f.inventory, self.f.key)
        from backend.database.models import BackgroundTask
        job = next(row for row in BackgroundTask.query.filter_by(kind='onboard') if row.payload['url'] == self.url)
        result = onboarding.onboard_page(job.payload, fetcher=lambda url, purpose:
            self.f.fixture.fetch(url, purpose) if purpose in ('article', 'body') else self.f.pages[self.url])
        self.assertEqual(result['state'], 'connected', result)
        self.sources = Department.query.filter_by(school_id=self.f.school.id, list_url=self.url, kind='column').order_by(Department.id).all()
        self.assertEqual({s.name for s in self.sources}, {'本科生通知', '研究生通知'})
        self.assertEqual(len(self.sources), 2)
        self.first, self.second = self.sources
        user = User.query.first(); user.role = 'admin'
        subscription = Subscription.query.one(); subscription.department_ids = [s.id for s in self.sources]
        announcement = Announcement.query.first()
        db.session.add(UserRead(user_id=user.id, announcement_id=announcement.id))
        db.session.add(UserAnnouncementState(user_id=user.id, announcement_id=announcement.id, starred=True))
        db.session.commit()
        self.client = self.f.fixture.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = user.id; session['_csrf_token'] = 'shared-test'
        self.endpoint = f'/api/schools/{self.f.school.id}/structure-snapshot'

    def get(self, **query):
        response = self.client.get(self.endpoint, query_string=query)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json

    def lifecycle(self, source, state):
        return self.client.post(f'/api/departments/{source.id}/lifecycle', json={'state': state},
            headers={'X-CSRF-Token': 'shared-test'})

    def retained(self):
        models = [Announcement, AnnouncementSource, UserRead, UserAnnouncementState,
            Subscription, SourceConfigVersion, SourceValidation]
        return {m.__tablename__: [tuple(getattr(row, col.name) for col in m.__table__.columns)
            for row in m.query.order_by(*m.__table__.primary_key.columns)] for m in models}

    def test_one_page_two_validated_columns_have_distinct_receipts_and_owners(self):
        graph = self.get()
        source_ids = {f'source:{s.id}' for s in self.sources}
        self.assertEqual({n['id'] for n in graph['nodes'] if n['entity_type'] == 'source'}, source_ids)
        pages = [n for n in graph['nodes'] if n['entity_type'] == 'page' and n.get('url') == self.url]
        self.assertEqual(len(pages), 1)
        self.assertEqual({r['target'] for r in graph['relations'] if r['type'] == 'source_on_page' and r['source'] == pages[0]['id']}, source_ids)
        self.assertEqual({r['target'] for r in graph['relations'] if r['type'] == 'source_ownership'}, source_ids)
        self.assertEqual(SourceConfigVersion.query.count(), 2)
        self.assertEqual(SourceValidation.query.filter(SourceValidation.department_id.in_([s.id for s in self.sources])).count(), 2)
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(AnnouncementSource.query.count(), 6)
        soup = BeautifulSoup(self.f.pages[self.url], 'lxml')
        for source in self.sources:
            rows = soup.select(source.list_selector)
            self.assertEqual(len(rows), 3)
            self.assertEqual({row.find_parent('section')['id'] for row in rows},
                {'ug' if source.name == '本科生通知' else 'pg'})
        capture = read_structure(self.f.school)
        self.assertEqual(capture['version'], graph['structure_version'])
        choices = source_choices(capture)['有依据的栏目归属']
        self.assertEqual({c['department'].id for g in choices for c in g['columns']}, {s.id for s in self.sources})
        self.assertEqual(set(source_paths(capture)), {s.id for s in self.sources})

    def test_soft_removal_and_restore_affect_only_the_selected_column(self):
        base = self.get(); saved = self.retained()
        for state in ('disabled', 'removed', 'active'):
            response = self.lifecycle(self.first, state)
            self.assertEqual(response.status_code, 200, response.text)
            graph = self.get()
            self.assertEqual(response.json['structure_version'], graph['structure_version'])
            ids = {n['id'] for n in graph['nodes'] if n['entity_type'] == 'source'}
            self.assertIn(f'source:{self.second.id}', ids)
            if state == 'active': self.assertIn(f'source:{self.first.id}', ids)
            else: self.assertNotIn(f'source:{self.first.id}', ids)
            self.assertEqual(self.retained(), saved)
            explorer = self.get(scope='exploration')
            nodes = {n['id']:n for n in explorer['nodes']}
            self.assertEqual(nodes[f'source:{self.first.id}']['state'], state if state != 'active' else 'connected')
            self.assertEqual(nodes[f'source:{self.second.id}']['state'], 'connected')
        update = self.get(since=base['structure_token'])
        self.assertEqual(update['update_mode'], 'delta')
        for field in ('nodes', 'relations'):
            rebuilt = {row['id']: row for row in base[field]}
            for ident in update['delta'][field]['remove']:
                rebuilt.pop(ident, None)
            rebuilt.update({row['id']: row for row in update['delta'][field]['upsert']})
            self.assertEqual(rebuilt, {row['id']: row for row in graph[field]})
        self.assertEqual(len({n['id'] for n in graph['nodes']}), len(graph['nodes']))
        version = current_version(self.f.school.id)
        self.assertFalse(self.lifecycle(self.first, 'active').json['changed'])
        self.assertEqual(current_version(self.f.school.id), version)

    def test_failed_lifecycle_change_does_not_publish_half_a_shared_page(self):
        before = self.get(); saved = self.retained()
        with patch.object(db.session, 'commit', side_effect=OperationalError('COMMIT', {}, Exception('injected write failure'))):
            response = self.lifecycle(self.first, 'removed')
        self.assertEqual(response.status_code, 500)
        after = self.get()
        for field in ('structure_version', 'structure_token', 'nodes', 'relations'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.retained(), saved)


if __name__ == '__main__':
    unittest.main()
