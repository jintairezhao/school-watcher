"""An installed publisher cannot be borrowed by a different page widget."""
import unittest

from backend.database.source_governance_models import SourceConfigVersion
from backend.services import source_governance as governance


class PublisherColumnContinuityTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_governance import SourceGovernanceTests
        self.fixture = SourceGovernanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        proposal = self.fixture.proposal()
        self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
        self.source = governance.activate_proposal(proposal.id)
        self.config = governance.source_config(self.source)

    def check_without_current_publisher_material(self, config, html, container='#notices'):
        evidence = governance.capture_source_evidence(self.fixture.school.id, config,
            department_id=self.source.id, seed_html=html, fetcher=self.fixture.fetcher(html),
            scope_evidence={'container_selector': container, 'heading_selector': container + ' h2'})
        self.assertEqual(evidence.bundle['identity_paths'], [])
        self.assertEqual(evidence.bundle['publisher_material'], {})
        proposal = governance.propose_source(self.fixture.school.id, config, evidence,
                                            department_id=self.source.id)
        result = governance.validate_proposal(proposal.id)
        self.assertEqual(result['item_count'], 3)
        return proposal, result

    def test_unchanged_widget_can_keep_previously_verified_publisher(self):
        _, result = self.check_without_current_publisher_material(self.config, self.fixture.html())
        self.assertTrue(result['passed'], result)

    def test_equivalent_css_for_the_same_complete_widget_keeps_publisher(self):
        config = dict(self.config, list_selector='section#notices > ul > li')
        _, result = self.check_without_current_publisher_material(config, self.fixture.html())
        self.assertTrue(result['passed'], result)

    def test_same_named_neighbour_with_identical_records_cannot_borrow_publisher(self):
        html = self.fixture.html() + self.fixture.html().replace('id="notices"', 'id="neighbour"')
        config = dict(self.config, list_selector='#neighbour li')
        proposal, result = self.check_without_current_publisher_material(config, html, '#neighbour')
        self.assertIn('publisher_requires_review', result['errors'])
        self.assertFalse(result['passed'])
        with self.assertRaises(ValueError):
            governance.activate_proposal(proposal.id)
        self.assertEqual(self.source.list_selector, '#notices li')
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_sibling_link_with_identical_text_and_url_cannot_borrow_publisher(self):
        html = self.fixture.html()
        for index in range(3):
            link = (f'<a href="article/{index}.htm">关于研究生报名事项的通知{index}</a>')
            html = html.replace(link, link + link)
        config = dict(self.config, title_selector='a:last-of-type', link_selector='a:last-of-type')
        _, result = self.check_without_current_publisher_material(config, html)
        self.assertIn('publisher_requires_review', result['errors'])
        self.assertFalse(result['passed'])


if __name__ == '__main__':
    unittest.main()
