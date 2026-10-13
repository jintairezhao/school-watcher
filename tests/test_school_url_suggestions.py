"""School identities must retain campus/college qualifiers when suggesting URLs."""
import unittest
from backend.scraper.discovery.university_urls import suggest_url


class SchoolURLSuggestionTests(unittest.TestCase):
    def test_reported_campus_is_not_the_main_university(self):
        for name in ('中国石油大学（北京）克拉玛依校区', '中国石油大学(北京) 克拉玛依校区'):
            self.assertEqual(suggest_url(name),'https://www.cupk.edu.cn')
        self.assertEqual(suggest_url('中国石油大学（北京）'),'https://www.cup.edu.cn')

    def test_separate_campuses_and_independent_colleges_keep_their_own_sites(self):
        for name,url in [('哈尔滨工业大学（深圳）','https://www.hitsz.edu.cn'),
                         ('山东大学（威海）','https://www.wh.sdu.edu.cn'),
                         ('厦门大学嘉庚学院','https://www.jgxy.xmu.edu.cn')]:
            self.assertEqual(suggest_url(name),url,name)

    def test_unknown_qualified_names_are_not_reduced_to_the_parent(self):
        for name in ('北京大学未知校区','浙江大学未知独立学院','上海交通大学未知分校','山东大学某研究院'):
            self.assertIsNone(suggest_url(name),name)

    def test_incomplete_or_ambiguous_input_does_not_pick_the_first_substring(self):
        for name in ('中国石油大学','北京','大学','中国','石油'):
            self.assertIsNone(suggest_url(name),name)

    def test_full_names_parentheses_and_explicit_aliases_still_work(self):
        self.assertEqual(suggest_url(' 上交 '),'https://www.sjtu.edu.cn')
        self.assertEqual(suggest_url('中国矿业大学(北京)'),'https://www.cumtb.edu.cn')
        self.assertEqual(suggest_url('天津工业大学'),'https://www.tiangong.edu.cn')


class LocalSchoolURLSuggestionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture=DirectOnboardingTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()

    def test_recorded_shared_root_is_valid_but_does_not_merge_campus_identities(self):
        import json
        from pathlib import Path
        from backend.services.school_registry import ensure_school
        from backend.scraper.discovery.university_urls import suggest_school
        path=Path(self.fixture.temp.name)/'campus-catalog.json'
        root='https://fixture.example.edu.cn/'
        entries=[{'name':'示例大学南校区','url':root},{'name':'示例大学北校区','url':root},
                 {'name':'示例大学独立学院','url':'https://college.example.edu.cn/'}]
        path.write_text(json.dumps({'schools':entries}),'utf8')
        self.fixture.app.config['STARTER_CATALOG_PATH']=str(path)
        ids=[]
        for entry in entries:
            self.assertEqual(suggest_school(entry['name']),entry)
            school,_=ensure_school(entry['name'],entry['url']);ids.append(school.id)
        self.assertEqual(len(set(ids)),3)
        self.assertIsNone(suggest_url('示例大学未知校区'))

    def test_explicit_local_name_and_alias_are_reused_without_removing_qualifiers(self):
        from backend.services.school_registry import ensure_school
        from backend.database.school_registry_models import SchoolRegistryEntry
        from backend.database.db import db
        school,_=ensure_school('示例大学独立学院','https://college.example.edu.cn/')
        entry=SchoolRegistryEntry.query.filter_by(school_id=school.id).one()
        entry.aliases=[*entry.aliases,'示例独立学院'];db.session.commit()
        self.assertEqual(suggest_url('示例大学 独立学院'),school.url)
        self.assertEqual(suggest_url('示例独立学院'),school.url)
        self.assertIsNone(suggest_url('示例大学其他独立学院'))

    def test_api_returns_actual_matched_identity_without_crawling_or_creating_tasks(self):
        from backend.database.models import BackgroundTask,User
        from unittest.mock import patch
        client=self.fixture.app.test_client()
        with client.session_transaction() as session:session['user_id']=User.query.one().id
        before=BackgroundTask.query.count()
        with patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model calls for address lookup')):
            data=client.get('/api/schools/suggest-url',query_string={'name':'中国石油大学(北京) 克拉玛依校区'}).json
            self.assertTrue(data['found']);self.assertEqual(data['url'],'https://www.cupk.edu.cn')
            self.assertIn('克拉玛依校区',data['matched_name'])
            self.assertFalse(client.get('/api/schools/suggest-url',query_string={'name':'中国石油大学未知学院'}).json['found'])
        self.assertEqual(BackgroundTask.query.count(),before)


if __name__=='__main__':unittest.main()
