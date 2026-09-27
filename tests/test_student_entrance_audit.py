"""Known central university entrances must not be hidden by shallow college reads."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.sources.audit_student_sources import classify_lead, CATEGORIES
from scripts.sources.read_student_entrances import choose_entrances


class StudentEntranceAuditTests(unittest.TestCase):
    def test_articles_mentioning_departments_or_majors_are_not_rosters(self):
        for label in ('院属教学机构负责人人选考察对象征', '关于本科专业设置调整的公示', '本科专业建设会议'):
            self.assertEqual(classify_lead({'label': label, 'state': 'pending'}), [])
        self.assertEqual(classify_lead({'label': '院系设置', 'state': 'pending'}), ['academic_directories'])

    def test_central_official_entrance_is_read_even_after_a_college_page(self):
        school = {'root_url': 'https://example.edu.cn/', 'student_sources': {
            category: {'status': 'not_located', 'leads': []} for category in CATEGORIES}}
        central = {'url': 'https://example.edu.cn/education/undergraduate/programmes.htm',
                   'state': 'pending', 'reference_urls': [school['root_url']]}
        college = {'url': 'https://a.example.edu.cn/p/', 'state': 'pending', 'reference_urls': []}
        school['student_sources']['major_directories'] = {'status': 'fetched_needs_review', 'leads': [college, central]}
        self.assertEqual(choose_entrances(school), [central])
        school['student_sources']['major_directories']['status'] = 'located_not_read'
        self.assertEqual(choose_entrances(school), [central])
        central['state'] = 'failed'
        self.assertEqual(choose_entrances(school), [college])

    def test_login_and_teaching_service_systems_are_not_public_notice_entrances(self):
        for label, url in [('教务系统', 'https://jwc.example.edu.cn/'),
                           ('教务在线', 'https://jwc.example.edu.cn/jsxsd/sso.jsp'),
                           ('教务管理', 'https://ids.example.edu.cn/authserver/login')]:
            self.assertEqual(classify_lead({'label': label, 'url': url, 'state': 'fetched'}), [])
        self.assertEqual(classify_lead({'label': '教务在线', 'url': 'https://jwc.example.edu.cn/',
                                       'state': 'fetched'}), ['teaching_entrances'])


if __name__ == '__main__':
    unittest.main()
