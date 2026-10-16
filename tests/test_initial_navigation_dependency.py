"""Homepage AI cannot preempt the initial layout pass while its reader holds a lock."""
import unittest

from backend.database.db import db
from backend.database.models import School
from backend.services import tasks
from backend.services.source_onboarding import _root_navigation_pending


class InitialNavigationDependencyTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests();self.fixture.setUp()
        self.school = db.session.get(School, self.fixture.school_id)
        self.parent = tasks.enqueue('discover', self.school.id, {'school_id': self.school.id})
        self.payload = {'parent_task_id': self.parent.id, 'parent_generation': self.parent.generation}

    def tearDown(self):
        self.fixture.tearDown()

    def navigation(self):
        return tasks.enqueue('navigation_review', 'root-layout', dict(self.payload,
            school_id=self.school.id, page={'url': self.school.url, 'kind': 'root'}))

    def test_reader_slice_and_unstarted_navigation_do_not_give_homepage_an_ai_head_start(self):
        self.assertTrue(_root_navigation_pending(self.payload, self.school, self.school.url))
        self.parent.checkpoint = {'first_slice_finished': True};db.session.commit()
        self.assertFalse(_root_navigation_pending(self.payload, self.school, self.school.url))
        self.navigation()
        self.assertTrue(_root_navigation_pending(self.payload, self.school, self.school.url))

    def test_first_cached_or_paid_route_cursor_releases_the_column_plan(self):
        nav = self.navigation();nav.checkpoint = {'route_cursor': 8};db.session.commit()
        self.assertFalse(_root_navigation_pending(self.payload, self.school, self.school.url))
        self.assertEqual(nav.generation, 1)
        self.assertEqual(self.parent.generation, 1)

    def test_direct_sources_other_units_stale_generations_and_completed_parents_are_not_held(self):
        self.assertFalse(_root_navigation_pending({}, self.school, self.school.url))
        self.assertFalse(_root_navigation_pending(self.payload, self.school, self.school.url + 'faculty/'))
        self.assertFalse(_root_navigation_pending(dict(self.payload, parent_generation=99), self.school, self.school.url))
        self.parent.state = 'done';db.session.commit()
        self.assertFalse(_root_navigation_pending(self.payload, self.school, self.school.url))


if __name__ == '__main__':
    unittest.main()
