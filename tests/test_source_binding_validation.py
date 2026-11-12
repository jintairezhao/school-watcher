"""Onboarding and graph publishing must use the same original website binding."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import gzip
import hashlib
import json
import unittest

from backend.database.db import db
from backend.database.models import Department
from backend.services import source_governance as governance
from backend.services.source_relationships import SourceRelationships

FIXTURES = Path(__file__).parent / 'fixtures/structure'


class SourceBindingValidationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_governance import SourceGovernanceTests
        self.fixture = SourceGovernanceTests()
        self.fixture.setUp()
        self.data = json.loads((FIXTURES / 'cupk-service-website-bindings-20261023.json').read_text('utf8'))
        self.materials = {url: gzip.decompress((FIXTURES / row['file']).read_bytes()).decode('utf8')
                          for url, row in self.data['materials'].items()}
        for url, html in self.materials.items():
            self.assertEqual(hashlib.sha256(html.encode()).hexdigest(), self.data['materials'][url]['sha256'])
        self.fixture.school.name = self.data['site']['name']
        self.fixture.school.url = self.data['site']['root_url']
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def bundle(self, source_id, group=None):
        source = next(row for row in self.data['sources'] if row['id'] == source_id)
        relationships = SourceRelationships({'site': self.data['site'], 'pages': self.data['pages']},
                                             self.data['records'], directory_navigation=True)
        paths = relationships.paths_for(source['list_url'])
        owners = relationships.publication_owners(source['list_url'], paths)
        config = {key: source.get(key, '') for key in governance.FIELDS}
        config['group_name'] = group if group is not None else next(iter(owners))
        references = [governance._snapshot(html, url, role='identity') for url, html in self.materials.items()]
        bundle = dict(school_id=self.fixture.school.id, root_url=self.fixture.school.url,
            school_name=self.fixture.school.name,
            list={'url': source['list_url']}, expected_config={},
            identity_paths=[path for path in paths if path['unit_name'] in owners],
            identity_site=self.data['site'], identity_pages=self.data['pages'], identity_snapshots=references)
        return config, bundle

    def errors(self, config, bundle):
        proposal = SimpleNamespace(department_id=None, id=-1)
        with patch('socket.socket.connect', side_effect=AssertionError('No website')), \
                patch('backend.ai.providers.complete', side_effect=AssertionError('No model')):
            return governance._identity_errors(proposal, config, bundle)

    def test_rostered_aid_center_without_url_uses_its_verified_service_website(self):
        config, bundle = self.bundle(1441)
        self.assertEqual(config['group_name'], '学生资助管理中心')
        self.assertEqual(self.errors(config, bundle), [])

    def test_existing_work_department_keeps_identity_at_its_second_website(self):
        config, bundle = self.bundle(1607)
        self.assertEqual(config['group_name'], '学生工作与安全保卫部')
        self.assertEqual(self.errors(config, bundle), [])

    def test_verified_other_publisher_is_a_conflict_not_missing_evidence(self):
        config, bundle = self.bundle(1441, group='教务部')
        self.assertEqual(self.errors(config, bundle), ['publisher_conflict'])

    def test_references_or_a_url_prefix_without_raw_binding_do_not_prove_owner(self):
        config, bundle = self.bundle(1441)
        bundle['identity_snapshots'] = [ref for ref in bundle['identity_snapshots']
                                        if ref['url'] != 'https://www.cupk.edu.cn/c/2017-04-21/485097.shtml']
        self.assertEqual(self.errors(config, bundle), ['publisher_requires_review'])

    def test_valid_row_cannot_be_assigned_to_another_unit_identity(self):
        config, bundle = self.bundle(1441)
        for path in bundle['identity_paths']:
            path['website_binding']['unit_key'] = 'different-roster-identity'
        self.assertEqual(self.errors(config, bundle), ['publisher_requires_review'])

    def test_same_row_binding_still_requires_current_metadata_hash(self):
        config, bundle = self.bundle(1607)
        bundle = deepcopy(bundle)
        for page in bundle['identity_pages']:
            if page['url'] == 'https://www.cupk.edu.cn/xsgl/':
                page['content_hash'] = 'f' * 64
        self.assertEqual(self.errors(config, bundle), ['publisher_requires_review'])

    def test_capture_retains_binding_context_but_does_not_activate_a_source(self):
        source = self.data['sources'][0]
        config = {key: source.get(key, '') for key in governance.FIELDS}
        config['group_name'] = '学生资助管理中心'
        catalog = SimpleNamespace(report=lambda _: {'site': self.data['site'], 'pages': self.data['pages']},
            structure=lambda _: self.data['records'], snapshot=lambda _, url: self.materials.get(url, ''),
            get_page=lambda _, url: next((page for page in self.data['pages'] if page['url'] == url), None))
        with patch('socket.socket.connect', side_effect=AssertionError('No website')):
            evidence = governance.capture_source_evidence(self.fixture.school.id, config,
                seed_html='<main>List material not supplied in this binding-only test.</main>', inventory=catalog,
                fetcher=lambda *_: (_ for _ in ()).throw(OSError('No retained sample')))
        self.assertTrue(evidence.bundle.get('identity_pages'))
        self.assertEqual(self.errors(config, evidence.bundle), [])
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(self.fixture.dept.name, '通知公告')


if __name__ == '__main__':
    unittest.main()
