"""An explicit profile subject may include its already known school's name."""
import unittest

from tests.test_profile_publisher_identity import UNIT, HOME, PROFILE
from tests.test_independent_publisher_identity import NAME, SCHOOL


class ProfileSchoolPrefixTests(unittest.TestCase):
    def roster_case(self, school):
        from tests.test_profile_publisher_identity import ProfilePublisherIdentityTests
        fixture = ProfilePublisherIdentityTests()
        fixture.setUp()
        try:
            body = ('<title>学院概况</title><nav><a href="'+HOME+'">首页</a></nav>'
                    '<div class="wp_articlecontent"><p>'+school+UNIT+
                    '成立于2015年，依托学校相关学科，开展人才培养、科学研究和公共服务工作。</p></div>')
            return fixture.case(profile=body)
        finally:
            fixture.doCleanups()

    def test_roster_profile_preserves_school_qualified_literal_subject(self):
        school = '中国石油大学（北京）克拉玛依校区'
        _, paths, assertions, _ = self.roster_case(school)
        proof = next((p for p in assertions if p), None)
        self.assertIsNotNone(proof)
        self.assertEqual(proof['identity_reference_url'], PROFILE)
        self.assertFalse(proof['profile_identity_witness']['identity_alias_asserted'])
        self.assertEqual(proof['profile_identity_witness']['unit_name'], UNIT)
        self.assertTrue(any((p.get('website_identity') or {}).get('entity_name') == school+UNIT for p in paths))

    def test_other_school_subject_is_not_an_exact_local_publisher(self):
        self.assertFalse(any(self.roster_case('另一所大学')[2]))

    def test_independent_declaration_checks_the_same_qualified_profile_subject(self):
        from tests.test_independent_publisher_identity import IndependentPublisherIdentityTests
        from tests.test_independent_publisher_identity import HOME as center_home, PROFILE as center_profile, SOURCE
        fixture = IndependentPublisherIdentityTests()
        fixture.setUp()
        try:
            home = ('<title>'+SCHOOL+NAME+'</title><nav><a href="'+center_profile+'">中心概况</a>'
                    '<a href="'+SOURCE+'">管理制度</a></nav>')
            profile = ('<title>中心概况</title><nav><a href="'+center_home+'">首页</a></nav>'
                       '<div class="wp_articlecontent"><p>'+SCHOOL+NAME+
                       '成立于2000年，承担本校教学实验、人才培养、科学研究与公共服务工作。</p></div>')
            _, _, _, paths, assertions, _ = fixture.case(home=home, profile=profile)
            proof = next((p for p in assertions if p), None)
            self.assertIsNotNone(proof)
            chosen = next(path for path, assertion in zip(paths, assertions) if assertion)
            self.assertEqual(chosen['unit_name'], NAME)
            self.assertEqual(chosen['publisher_declaration']['profile_claim']['entity_name'], SCHOOL+NAME)
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
