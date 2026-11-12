"""New captured directory DOM and source activation share one visible commit."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from sqlalchemy import event

from backend.database.db import db
from backend.database.models import CatalogPublication, Department, DepartmentDirectoryEntry
from backend.database.source_governance_models import SourceProposal, SourceConfigVersion
from backend.scraper.discovery.inventory_crawler import _inspect_page
from backend.services import source_governance as governance
from backend.services.structure_versions import current_version
from tests import test_school_structure as fixture


class SourceMaterialActivationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SchoolStructureTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.config = dict(name='通知公告', group_name='计算机学院', list_url=fixture.CS + 'notices/',
            list_selector='#notices li', title_selector='a', link_selector='a', date_selector='span', content_selector='article')
        self.stamp = '2026-10-01T06:00:00+00:00'
        for url, kind, label in [(fixture.ROOT, 'root', '测试大学'),
                (fixture.ROOT + 'colleges/', 'directory', '院系设置'), (fixture.CS, 'unit', '计算机学院'),
                (fixture.CS + 'notices/', 'channel', '通知公告')]:
            self.f.inventory.enqueue(self.f.key, url, label, kind, 1, [], 'school_domain')
            _inspect_page(self.f.inventory, self.f.inventory.report(self.f.key)['site'],
                self.f.inventory.get_page(self.f.key, url), ai_assist=False,
                fetcher=lambda address: dict(url=address, status=200, html=self.f.pages[address], checked_at=self.stamp))

    def proposal(self):
        evidence = governance.capture_source_evidence(self.f.school.id, self.config, inventory=self.f.inventory,
            seed_html=fixture.HTML, fetcher=lambda url, purpose: self.f.fixture.fetch(url, purpose) if purpose == 'body' else fixture.HTML)
        proposal = governance.propose_source(self.f.school.id, self.config, evidence, origin='manual_review')
        result = governance.validate_proposal(proposal.id)
        self.assertTrue(result['passed'], result)
        return proposal

    def owners(self, source):
        return [fact for row in DepartmentDirectoryEntry.query.filter_by(department_id=source.id)
                for fact in row.facts_json if fact['type'] == 'source_ownership']

    def test_shared_directory_keeps_its_observed_name_when_a_college_activates(self):
        address=fixture.ROOT+'colleges/'
        observed=self.f.inventory.get_page(self.f.key,address)['label']
        self.assertEqual(observed,'院系设置')
        # Like the retained campus directory, this page has no local heading;
        # its name comes from the already observed official directory entry.
        self.f.pages[address]=self.f.pages[address].replace('<h1>院系设置</h1>','')
        _inspect_page(self.f.inventory,self.f.inventory.report(self.f.key)['site'],
            self.f.inventory.get_page(self.f.key,address),ai_assist=False,
            fetcher=lambda url:dict(url=url,status=200,html=self.f.pages[url],checked_at=self.stamp))
        initial=self.proposal()
        bundle=json.loads(initial.evidence_json)
        refs={r['url']:r for r in bundle['identity_snapshots']}
        root=governance._snapshot(self.f.pages[fixture.ROOT],fixture.ROOT,role='identity')
        root['observed_at']=self.stamp
        bundle['publisher_material']=dict(root=root,directory=refs[address],home=refs[fixture.CS])
        bundle['publisher_evidence']=dict(name=self.config['group_name'],directory_evidence_id='directory',homepage_evidence_id='home')
        from backend.services.source_workflow import publisher_proven
        self.assertTrue(publisher_proven(bundle,self.config))
        proposal=governance.propose_source(self.f.school.id,self.config,governance.CapturedEvidence(bundle))
        self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
        from backend.services.source_material_publication import _materials
        directory=next(row for row in _materials(self.f.school,bundle,self.config,self.f.catalog) if row['url']==address)
        self.assertEqual(directory['label'],observed)
        governance.activate_proposal(proposal.id)
        published=self.f.catalog.pin(self.f.key).get_page(self.f.key,address)
        self.assertEqual(published['label'],observed)
        self.assertNotEqual(published['label'],self.config['group_name'])
        directory_groups=[n['name'] for n in self.f.catalog.pin(self.f.key).structure(self.f.key)
            if n['reference_url']==address and n['relation']=='page_identity']
        self.assertEqual(directory_groups,[observed])

    def test_unpublished_material_config_and_owner_have_only_one_visible_commit(self):
        proposal = self.proposal()
        self.assertEqual(CatalogPublication.query.count(), 0)
        before = current_version(self.f.school.id)
        commits = []
        database = Path(db.engine.url.database).resolve().as_uri() + '?mode=ro'
        def observed_commit(_):
            with closing(sqlite3.connect(database, uri=True)) as reader:
                facts = [fact for row in reader.execute('select facts_json from department_directory_entries') for fact in json.loads(row[0])]
                commits.append(dict(pointers=reader.execute('select count(*) from catalog_publications').fetchone()[0],
                    sources=reader.execute("select count(*) from departments where list_selector='#notices li'").fetchone()[0],
                    ownership=any(f['type'] == 'source_ownership' for f in facts)))
        session = db.session()
        event.listen(session, 'after_commit', observed_commit)
        try:
            with patch('backend.services.source_governance._fetch', side_effect=AssertionError('no network')):
                source = governance.activate_proposal(proposal.id)
        finally:
            event.remove(session, 'after_commit', observed_commit)
        self.assertEqual(commits, [dict(pointers=1, sources=1, ownership=True)])
        self.assertTrue(self.owners(source))
        self.assertEqual(current_version(self.f.school.id)['revision'], before['revision'] + 1)
        catalog = self.f.catalog.pin(self.f.key)
        self.assertEqual(catalog.snapshot(self.f.key, fixture.ROOT + 'colleges/'), self.f.pages[fixture.ROOT + 'colleges/'])
        nodes = [n for n in catalog.structure(self.f.key) if n['reference_url'] == fixture.ROOT + 'colleges/']
        self.assertTrue(nodes)
        self.assertTrue(all(n['observed_at'] == self.stamp for n in nodes))
        self.assertEqual(catalog.monitor_snapshot(self.f.key)['exploration_events'], [])
        pointer = db.session.get(CatalogPublication, self.f.key).generation
        version = current_version(self.f.school.id)
        self.assertEqual(governance.activate_proposal(proposal.id).id, source.id)
        self.assertEqual(db.session.get(CatalogPublication, self.f.key).generation, pointer)
        self.assertEqual(current_version(self.f.school.id), version)
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_failure_after_structure_staging_keeps_old_pointer_and_all_source_state(self):
        # Publish only the school homepage, leaving the captured roster unpublished.
        from backend.services.source_inventory import Inventory
        old = Inventory(Path(self.f.fixture.temp.name) / 'old.sqlite3')
        old.ensure_site(self.f.school.name, fixture.ROOT)
        old.finish(self.f.key, fixture.ROOT, state='fetched', html=self.f.pages[fixture.ROOT], fetched_at=self.stamp)
        self.f.catalog.publish(old, self.f.key)
        proposal = self.proposal(); ident = proposal.id
        pointer = db.session.get(CatalogPublication, self.f.key).generation
        version = current_version(self.f.school.id)
        from backend.services.school_structure import sync_official_structure
        def fail(*args, **kwargs):
            sync_official_structure(*args, **kwargs)
            raise RuntimeError('after material and ownership staging')
        with patch('backend.services.school_structure.sync_official_structure', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'after material'):
                governance.activate_proposal(ident)
        db.session.commit()
        self.assertEqual(db.session.get(CatalogPublication, self.f.key).generation, pointer)
        self.assertEqual(current_version(self.f.school.id), version)
        self.assertEqual(db.session.get(SourceProposal, ident).state, 'validated')
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertFalse(Department.query.filter_by(list_selector='#notices li').count())
        self.assertIsNone(self.f.catalog.pin(self.f.key).get_page(self.f.key, fixture.ROOT + 'colleges/'))
        self.assertTrue(self.owners(governance.activate_proposal(ident)))

    def test_missing_raw_identity_material_cannot_publish_a_candidate_path(self):
        proposal = self.proposal()
        bundle = json.loads(proposal.evidence_json)
        reference = next(r for r in bundle['identity_snapshots'] if r['url'] == fixture.ROOT + 'colleges/')
        path = governance._evidence_root() / (reference['hash'] + '.html.gz')
        path.unlink()
        with self.assertRaisesRegex(ValueError, '证据不可用'):
            governance.activate_proposal(proposal.id)
        db.session.commit()
        self.assertEqual(CatalogPublication.query.count(), 0)
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_newer_independent_list_is_the_published_material_not_the_old_first_read(self):
        latest = fixture.HTML.replace('</body>', '<p>new independent observation</p></body>')
        evidence = governance.capture_source_evidence(self.f.school.id, self.config, inventory=self.f.inventory,
            seed_html=fixture.HTML, fetcher=lambda url, purpose: self.f.fixture.fetch(url, purpose)
                if purpose == 'body' else latest if purpose == 'independent_list' else fixture.HTML)
        self.assertNotEqual(evidence.bundle['list']['hash'], evidence.bundle['independent']['hash'])
        proposal = governance.propose_source(self.f.school.id, self.config, evidence, origin='manual_review')
        result = governance.validate_proposal(proposal.id)
        self.assertTrue(result['passed'], result)
        source = governance.activate_proposal(proposal.id)
        catalog = self.f.catalog.pin(self.f.key)
        self.assertEqual(catalog.snapshot(self.f.key, self.config['list_url']), latest)
        self.assertEqual(catalog.get_page(self.f.key, self.config['list_url'])['content_hash'], evidence.bundle['independent']['hash'])
        self.assertTrue(self.owners(source))
        saved = json.loads(db.session.get(SourceProposal, proposal.id).evidence_json)
        self.assertEqual(saved['list']['hash'], evidence.bundle['list']['hash'])
        self.assertEqual(saved['independent']['hash'], evidence.bundle['independent']['hash'])

    def test_unrelated_missing_navigation_dom_does_not_hide_executed_publisher_proof(self):
        evidence = governance.capture_source_evidence(self.f.school.id, self.config, inventory=self.f.inventory,
            seed_html=fixture.HTML, fetcher=lambda url, purpose: self.f.fixture.fetch(url, purpose) if purpose == 'body' else fixture.HTML)
        self.assertTrue(evidence.bundle['identity_paths'])
        for path in evidence.bundle['identity_paths']:
            path['references'].append(dict(url=fixture.CS+'unrelated/', content_hash='0'*64, locators=['nav a']))
        proposal = governance.propose_source(self.f.school.id, self.config, evidence, origin='manual_review')
        result = governance.validate_proposal(proposal.id)
        self.assertTrue(result['passed'], result)

    def test_hash_consistent_but_false_publisher_identity_does_not_pass(self):
        evidence = governance.capture_source_evidence(self.f.school.id, self.config, inventory=self.f.inventory,
            seed_html=fixture.HTML, fetcher=lambda url, purpose: self.f.fixture.fetch(url, purpose) if purpose == 'body' else fixture.HTML)
        wrong = governance._snapshot(self.f.pages[fixture.CS].replace('计算机学院', '化学学院'), fixture.CS, role='identity')
        evidence.bundle['identity_snapshots'] = [wrong if ref['url'] == fixture.CS else ref for ref in evidence.bundle['identity_snapshots']]
        evidence.bundle['publisher_material'] = {key: wrong if ref['url'] == fixture.CS else ref
            for key, ref in evidence.bundle.get('publisher_material', {}).items()}
        for path in evidence.bundle['identity_paths']:
            for reference in path['references']:
                if reference['url'] == fixture.CS: reference['content_hash'] = wrong['hash']
        proposal = governance.propose_source(self.f.school.id, self.config, evidence, origin='manual_review')
        self.assertIn('publisher_requires_review', governance.validate_proposal(proposal.id)['errors'])

    def test_newer_published_directory_is_not_overwritten_by_old_proposal(self):
        proposal = self.proposal()
        self.f.inventory.finish(self.f.key, fixture.ROOT + 'colleges/', state='fetched',
            html='<h1>院系设置</h1><p>已更新</p>', fetched_at='2026-10-25T06:00:00+00:00')
        self.f.catalog.publish(self.f.inventory, self.f.key)
        pointer = db.session.get(CatalogPublication, self.f.key).generation
        with self.assertRaisesRegex(ValueError, '更新的材料'):
            governance.activate_proposal(proposal.id)
        self.assertEqual(db.session.get(CatalogPublication, self.f.key).generation, pointer)
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_pointer_write_failure_rolls_back_already_staged_config_and_owner(self):
        proposal = self.proposal(); ident = proposal.id
        before = current_version(self.f.school.id)
        def reject_pointer(connection, cursor, statement, parameters, context, many):
            if statement.lower().startswith('insert into catalog_publications'):
                self.assertEqual(SourceConfigVersion.query.count(), 1)
                self.assertTrue(any(f['type'] == 'source_ownership' for row in DepartmentDirectoryEntry.query.all() for f in row.facts_json))
                raise RuntimeError('injected pointer failure')
        event.listen(db.engine, 'before_cursor_execute', reject_pointer)
        try:
            with self.assertRaisesRegex(RuntimeError, 'pointer failure'):
                governance.activate_proposal(ident)
        finally:
            event.remove(db.engine, 'before_cursor_execute', reject_pointer)
        db.session.commit()
        self.assertEqual(CatalogPublication.query.count(), 0)
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertEqual(db.session.get(SourceProposal, ident).state, 'validated')
        self.assertEqual(current_version(self.f.school.id), before)
        self.assertTrue(self.owners(governance.activate_proposal(ident)))


if __name__ == '__main__':
    unittest.main()
