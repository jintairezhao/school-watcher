"""Independent regressions for discovery recovery and source identity gates."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import create_app
from backend.database.db import db
from backend.database.models import Department, School
from backend.database.source_governance_models import SourceConfigVersion, SourceReviewEvent
from backend.services import source_governance as governance


class SourceGovernanceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='watcher-governance-')
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'isolated-review',
            'SQLALCHEMY_DATABASE_URI': 'sqlite://',
            'SOURCE_INVENTORY_PATH': Path(self.tmp.name) / 'inventory.sqlite3',
            'SOURCE_GOVERNANCE_EVIDENCE_PATH': Path(self.tmp.name) / 'evidence',
            'SOURCE_CATALOG_PATH': Path(self.tmp.name) / 'catalog.sqlite3'})
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.school = School(name='示例大学', url='https://www.example.edu.cn/')
        db.session.add(self.school); db.session.flush()
        self.dept = Department(school_id=self.school.id, name='通知公告', group_name='示例学院',
            list_url='https://college.example.edu.cn/notices/', list_selector='#notices li',
            title_selector='a', link_selector='a', date_selector='time')
        db.session.add(self.dept); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    @staticmethod
    def html():
        return '<section id="notices"><h2>通知公告</h2><ul>' + ''.join(
            f'<li><a href="article/{i}.htm">关于研究生报名事项的通知{i}</a><time>2026-09-24</time></li>'
            for i in range(3)) + '</ul></section>'

    def fetch(self, url, purpose):
        if purpose == 'body':
            index = url.rsplit('/', 1)[-1].split('.')[0]
            return ('<article><h1>关于研究生报名事项的通知' + index + '</h1>'
                    '<p>请符合条件的同学认真阅读本通知，并按照学校要求在指定时间提交报名材料。</p></article>')
        return self.html()

    def test_metadata_only_department_does_not_prove_publisher_identity(self):
        # A normal import/YAML seed can create this row while keeping its rules
        # inactive. Matching the row's unverified labels cannot become proof.
        config = governance.source_config(self.dept)
        self.dept.list_selector = ''
        db.session.commit()
        evidence = governance.capture_source_evidence(self.school.id, config,
            department_id=self.dept.id, seed_html=self.html(), fetcher=self.fetch)
        proposal = governance.propose_source(self.school.id, config, evidence,
            department_id=self.dept.id)
        validation = governance.validate_proposal(proposal.id)
        self.assertEqual(validation['item_count'], 3)
        self.assertIn('publisher_requires_review', validation['errors'])
        with self.assertRaises(ValueError):
            governance.activate_proposal(proposal.id)
        self.assertEqual(self.dept.list_selector, '')
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_committed_validation_resumes_activation_without_network_or_ai(self):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.services.source_catalog import publication_candidates
        inventory = Inventory(Path(self.tmp.name) / 'discovery.sqlite3')
        key = inventory.ensure_site(self.school.name, self.school.url)
        crawl_site(inventory, key, max_pages=1,
            fetcher=lambda url: {'html': self.html(), 'url': url, 'status': 200})
        candidate = publication_candidates(inventory.report(key), inventory.structure(key), focus='all')[0]
        config = {field: candidate.get(field, '') for field in governance.FIELDS}
        evidence = governance.capture_source_evidence(self.school.id, config,
            seed_html=self.html(), inventory=inventory, fetcher=self.fetch)
        proposal = governance.propose_source(self.school.id, config, evidence, origin='discovery')
        validation = governance.validate_proposal(proposal.id)
        self.assertTrue(validation['passed'], validation)
        proposal_id = proposal.id
        school_id = self.school.id
        # End the first worker session after validation was committed but before
        # activation. The next discovery pass sees only durable state.
        db.session.remove()
        with patch.object(governance, 'capture_source_evidence', side_effect=AssertionError('unexpected network')), \
             patch.object(governance, 'run_source_skill_for_proposal', side_effect=AssertionError('unexpected paid call')):
            result = governance.process_discovered_candidates(school_id, inventory, key)
        self.assertEqual(result['proposal_ids'], [proposal_id])
        self.assertEqual(len(result['activated_ids']), 1)
        self.assertEqual(result['remaining_candidates'], 0)
        self.assertEqual(SourceConfigVersion.query.count(), 1)
        self.assertEqual(Department.query.count(), 2)
        self.assertEqual(governance.process_discovered_candidates(school_id, inventory, key)['activated_ids'], [])
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_old_selector_cannot_override_current_official_publisher_evidence(self):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        inventory = Inventory(Path(self.tmp.name) / 'ownership.sqlite3')
        key = inventory.ensure_site(self.school.name, self.school.url)
        with inventory.connect() as connection:
            connection.execute("UPDATE pages SET kind='directory',label='院系设置' WHERE site_key=?", (key,))
        pages = {
            self.school.url: '<main><h4><a href="https://college.example.edu.cn/">外国语学院</a></h4></main>',
            'https://college.example.edu.cn/': '<title>示例大学外国语学院</title><nav><a href="notices/">通知公告</a></nav>',
            self.dept.list_url: self.html(),
        }
        crawl_site(inventory, key, max_pages=3,
            fetcher=lambda url: {'html': pages.get(url, ''), 'url': url, 'status': 200})
        config = governance.source_config(self.dept)
        evidence = governance.capture_source_evidence(self.school.id, config,
            department_id=self.dept.id, seed_html=self.html(), inventory=inventory, fetcher=self.fetch)
        self.assertEqual({path['unit_name'] for path in evidence.bundle['identity_paths']}, {'外国语学院'})
        self.assertEqual(len(evidence.bundle['identity_snapshots']), 2)
        proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
        validation = governance.validate_proposal(proposal.id)
        self.assertIn('publisher_conflict', validation['errors'])
        with self.assertRaises(ValueError):
            governance.activate_proposal(proposal.id)
        self.assertEqual(self.dept.group_name, '示例学院')
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_failed_model_revisions_have_a_durable_two_call_limit(self):
        config = governance.source_config(self.dept)
        config['list_selector'] = '#missing li'
        evidence = governance.capture_source_evidence(self.school.id, config,
            department_id=self.dept.id, seed_html=self.html(), fetcher=self.fetch)
        proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
        governance.validate_proposal(proposal.id)
        response = {'status': 'succeeded', 'output': {'proposals': [{
            'candidate_id': 'source-' + str(proposal.id), 'decision': 'propose', 'config': config}]}}
        proposal_id = proposal.id
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value=response) as skill, \
             patch.object(governance, '_fetch', side_effect=self.fetch):
            governance.run_source_skill_for_proposal(proposal_id)
            db.session.remove()
            result = governance.run_source_skill_for_proposal(proposal_id)
        self.assertEqual(result['status'], 'review_required')
        self.assertEqual(skill.call_count, 2)
        events = SourceReviewEvent.query.filter_by(proposal_id=proposal_id, action='skill_attempt').all()
        self.assertEqual(len(events), 2)
        self.assertTrue(all(json.loads(event.detail_json)['status'] == 'succeeded' for event in events))
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_interrupted_attempt_reuses_execution_identity_and_uncertainty_stops_retry(self):
        class WorkerExit(BaseException):
            pass
        config = governance.source_config(self.dept)
        evidence = governance.capture_source_evidence(self.school.id, config,
            department_id=self.dept.id, seed_html=self.html(), fetcher=self.fetch)
        proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
        proposal_id = proposal.id
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=[WorkerExit(), {'status': 'uncertain'}]) as skill:
            with self.assertRaises(WorkerExit):
                governance.run_source_skill_for_proposal(proposal_id)
            db.session.remove()
            self.assertEqual(governance.run_source_skill_for_proposal(proposal_id)['status'], 'uncertain')
            self.assertEqual(governance.run_source_skill_for_proposal(proposal_id)['status'], 'result_pending_review')
        self.assertEqual(skill.call_count, 2)
        self.assertEqual(skill.call_args_list[0].args[4], skill.call_args_list[1].args[4])
        event = SourceReviewEvent.query.filter_by(proposal_id=proposal_id, action='skill_attempt').one()
        self.assertEqual(json.loads(event.detail_json)['status'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
