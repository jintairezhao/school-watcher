"""Chosen and composite identities change exploration hints without new requests."""
import itertools
import unittest
from unittest.mock import patch

from backend.scraper.discovery.attention import compose_profiles, PROFILE_CATEGORIES, candidate_attention
from backend.services.discovery_profiles import validate_profiles, stage_profiles, user_profiles
from backend.services.discovery_policy import get_attention_policy


class CompositeProfileTests(unittest.TestCase):
    def test_all_seven_combinations_keep_unique_and_shared_needs(self):
        roles = tuple(PROFILE_CATEGORIES)
        keys = set()
        for size in (1,2,3):
            for group in itertools.combinations(roles,size):
                result = compose_profiles(list(group)); keys.add(result['key'])
                expected = set().union(*(set(PROFILE_CATEGORIES[role]) for role in group))
                self.assertEqual(set(result['categories']), expected)
                self.assertEqual(set(result['shared_categories']), {need for need in expected
                    if sum(need in PROFILE_CATEGORIES[role] for role in group) > 1})
                self.assertTrue(all(1 <= weight <= 1.25 for weight in result['weights'].values()))
        self.assertEqual(len(keys), 7)

    def test_repeated_roles_and_order_do_not_duplicate_the_composite(self):
        self.assertEqual(compose_profiles(['teacher','graduate','teacher']), compose_profiles(['graduate','teacher']))
        for value in (None, [], '', 'teacher', ['unknown'], [True], {}, ['teacher',1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_profiles(value)

    def test_graduate_teacher_preserves_funding_and_personnel_and_prioritizes_shared_research(self):
        result = compose_profiles(['graduate','teacher'])
        self.assertIn('funding', result['categories']); self.assertIn('personnel',result['categories'])
        self.assertEqual(result['weights']['funding'], 1)
        self.assertEqual(result['weights']['personnel'], 1)
        self.assertEqual(result['weights']['research'], 1.25)
        for label in ('奖学金','教师职称','科研项目'):
            self.assertGreater(candidate_attention({'url':'https://example.edu.cn/'+label,'label':label},
                profiles=result['profiles'],weights=result['weights'])['score'],0)


class SubscriberProfileTests(unittest.TestCase):
    def setUp(self):
        import test_shared_summaries as fixtures
        from backend.database.models import School
        from backend.database.db import db
        self.fixture=fixtures.SharedSummaryTests(); self.fixture.setUp()
        self.school=db.session.get(School,self.fixture.ann.school_id)

    def tearDown(self):
        self.fixture.tearDown()

    def subscribe(self,user,roles,school=None):
        from backend.database.models import Subscription
        from backend.database.db import db
        school=school or self.school
        db.session.add(Subscription(user_id=user.id,school_id=school.id))
        stage_profiles(user.id,roles); db.session.commit()

    def test_school_policy_uses_actual_subscriber_profiles_and_merges_combinations(self):
        self.subscribe(self.fixture.reader,['graduate','teacher'])
        value=get_attention_policy(self.school.id)
        self.assertEqual(value['profiles'], ['graduate','teacher'])
        self.assertEqual(value['weights']['research'],1.25)
        self.assertEqual(value['profile_context']['groups'][0]['label'],'研究生＋教师')
        self.subscribe(self.fixture.admin,['undergraduate'])
        combined=get_attention_policy(self.school.id)
        self.assertEqual(combined['profiles'],['undergraduate','graduate','teacher'])
        self.assertEqual(combined['profile_context']['subscriber_count'],2)
        self.assertEqual(combined['weights']['teaching'],1.125)
        self.assertEqual(combined['weights']['funding'],1)

    def test_other_school_and_unconfigured_subscribers_do_not_replace_a_known_profile(self):
        from backend.database.models import School,Subscription
        from backend.database.db import db
        other=School(name='另一所学校',url='https://other.example.edu.cn',enabled=True)
        db.session.add(other); db.session.commit()
        self.subscribe(self.fixture.reader,['teacher'])
        db.session.add(Subscription(user_id=self.fixture.admin.id,school_id=self.school.id)); db.session.commit()
        self.assertEqual(get_attention_policy(self.school.id)['profiles'],['teacher'])
        stage_profiles(self.fixture.admin.id,['undergraduate']); db.session.commit()
        # Move the second person's subscription: another school cannot broaden this school's portrait.
        Subscription.query.filter_by(user_id=self.fixture.admin.id).update({'school_id':other.id}); db.session.commit()
        self.assertEqual(get_attention_policy(self.school.id)['profiles'],['teacher'])
        self.assertEqual(get_attention_policy(other.id)['profiles'],['undergraduate'])
        self.assertEqual(get_attention_policy(root_url=self.school.url)['profiles'],['teacher'])

    def test_worker_and_actual_frontier_use_selection_without_request_or_budget_reset(self):
        from backend.services.source_inventory import Inventory
        from backend.services import tasks
        from backend.database.models import BackgroundTask
        from backend.database.db import db
        self.subscribe(self.fixture.reader,['teacher'])
        store=Inventory(self.fixture.temp.name + '/frontier.sqlite3')
        key=store.ensure_site(self.school.name,self.school.url)
        store.finish(key,self.school.url+'/',html='<title>学校官网</title>',state='fetched',status_code=200)
        store.enqueue(key,self.school.url+'/staff','教师职称评审','channel',2,['教师发展'],'school_domain')
        store.enqueue(key,self.school.url+'/aid','奖学金通知','channel',2,['学生资助'],'school_domain')
        with patch('backend.ai.providers.complete',side_effect=AssertionError('No paid request')):
            self.assertEqual(store.claim(key,focus='valuable')['url'],self.school.url+'/staff')
        with tasks.execution_scope({'payload':{'school_id':self.school.id}}):
            self.assertEqual(get_attention_policy()['profiles'],['teacher'])
        self.assertEqual(BackgroundTask.query.count(),0)
        db.session.commit()

    def test_changing_profiles_keeps_existing_budget_and_saved_task_identity(self):
        from backend.ai.models import AIBudget,AISchoolRound
        from backend.database.db import db
        from backend.database.models import BackgroundTask
        self.subscribe(self.fixture.reader,['undergraduate'])
        key=f'school-discovery:{self.school.id}:1:1'
        ledger=AIBudget(key=key,used_tokens=123456,reserved_tokens=500,used_requests=19,reserved_requests=1)
        row=BackgroundTask(identity=f'discover:{self.school.id}',kind='discover',state='waiting',
            generation=3,payload={'school_id':self.school.id},checkpoint={'route_cursor':7,'paid_identity':'keep'})
        db.session.add_all([ledger,row,AISchoolRound(school_id=self.school.id,budget_key=key)])
        db.session.commit(); ident=row.id
        with patch('backend.ai.providers.complete',side_effect=AssertionError('No paid requests')):
            stage_profiles(self.fixture.reader.id,['graduate','teacher']); db.session.commit()
            self.assertEqual(get_attention_policy(self.school.id)['profiles'],['graduate','teacher'])
        db.session.refresh(ledger); db.session.refresh(row)
        self.assertEqual((ledger.used_tokens,ledger.reserved_tokens,ledger.used_requests,ledger.reserved_requests),
                         (123456,500,19,1))
        self.assertEqual((row.id,row.generation,row.checkpoint),(ident,3,{'route_cursor':7,'paid_identity':'keep'}))


class ProfileSetupTests(unittest.TestCase):
    def setUp(self):
        from test_collection_setup import CollectionSetupTests
        self.fixture=CollectionSetupTests(); self.fixture.setUp()
        self.client=self.fixture.client; self.headers=self.fixture.headers

    def tearDown(self):
        self.fixture.tearDown()

    def save(self,profiles,**changes):
        data={'since_date':'2024-06-15','interval':30,'ai_mode':'rules','profiles':profiles}
        data.update(changes)
        return self.client.post('/api/collection/setup',json=data,headers=self.headers)

    def owner(self):
        from backend.auth.desktop import ensure_local_owner
        return ensure_local_owner()

    def test_save_reload_and_legacy_edits_preserve_the_current_users_selection(self):
        from backend.database.models import BackgroundTask
        self.assertEqual(self.save(['teacher','graduate','teacher']).status_code,200)
        self.assertEqual(user_profiles(self.owner().id),['graduate','teacher'])
        page=self.client.get('/collection/setup')
        self.assertIn('name="profiles" value="graduate" checked',page.text)
        self.assertIn('name="profiles" value="teacher" checked',page.text)
        self.assertNotIn('name="profiles" value="undergraduate" checked',page.text)
        self.assertEqual(self.client.post('/api/collection/setup',json={
            'since_date':'2024-06-15','interval':45,'ai_mode':'rules'},headers=self.headers).status_code,200)
        self.assertEqual(user_profiles(self.owner().id),['graduate','teacher'])
        self.assertEqual(BackgroundTask.query.count(),0)

    def test_invalid_profile_or_other_setting_does_not_partially_save(self):
        from backend.database.models import AppConfig
        for profiles in ([],['unknown'],'teacher',[False],None):
            self.assertEqual(self.save(profiles).status_code,400)
            self.assertIsNone(AppConfig.get('scrape_interval'))
            self.assertIsNone(user_profiles(self.owner().id))
        self.assertEqual(self.save(['teacher'],interval=1).status_code,400)
        self.assertIsNone(user_profiles(self.owner().id))

    def test_reader_can_save_own_profile_without_changing_shared_settings_or_admin(self):
        from backend.database.db import db
        from backend.database.models import User,AppConfig
        admin_id=self.owner().id
        self.assertEqual(self.save(['teacher']).status_code,200)
        reader=User(username='profile-reader',password_hash='unused',role='user')
        db.session.add(reader); db.session.commit()
        with patch('backend.auth.desktop.local_owner',return_value=reader):
            result=self.client.post('/api/collection/setup',json={'ai_mode':'rules','profiles':['undergraduate'],
                'user_id':admin_id},headers=self.headers)
            self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(user_profiles(reader.id),['undergraduate'])
        self.assertEqual(user_profiles(admin_id),['teacher'])
        self.assertEqual(AppConfig.get('scrape_interval'),'30')


if __name__ == '__main__':
    unittest.main()
