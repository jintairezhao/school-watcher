"""Locale, education system and school metadata stay independent."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from backend.services.localization import preferences,stage_preferences,school_context,english_catalog
from backend.scraper.discovery.attention import candidate_attention
from backend.scraper.discovery.directory_terms import academic_collection_label


class LocaleFrameworkTests(unittest.TestCase):
    def setUp(self):
        from test_collection_setup import CollectionSetupTests
        self.fixture=CollectionSetupTests(); self.fixture.setUp()
        self.client=self.fixture.client; self.headers=self.fixture.headers

    def tearDown(self): self.fixture.tearDown()

    def owner(self):
        from backend.auth.desktop import ensure_local_owner
        return ensure_local_owner()

    def test_switch_persists_without_changing_identity_education_or_starting_work(self):
        from backend.database.models import BackgroundTask
        from backend.database.db import db
        from backend.services.discovery_profiles import stage_profiles,user_profiles
        owner=self.owner(); stage_profiles(owner.id,['graduate','teacher']); db.session.commit()
        with patch('backend.ai.providers.complete',side_effect=AssertionError('No paid request')):
            response=self.client.post('/api/preferences/localization',json={'locale':'en'},headers=self.headers)
            self.assertEqual(response.status_code,200)
            page=self.client.get('/collection/setup')
        self.assertIn('<html lang="en">',page.text)
        self.assertEqual(page.headers['Content-Language'],'en')
        self.assertIn('Your identities and interests',page.text)
        self.assertIn('Graduate student',page.text)
        self.assertEqual(preferences(owner.id)['education_system'],'cn')
        self.assertEqual(user_profiles(owner.id),['graduate','teacher'])
        self.assertEqual(BackgroundTask.query.count(),0)

    def test_invalid_settings_and_missing_csrf_do_not_save(self):
        for value in ({'locale':'fr'},{'time_zone':'Not/AZone'},{'education_system':'US'},
                      {'user_id':999,'locale':'en'},['en']):
            response=self.client.post('/api/preferences/localization',json=value,headers=self.headers)
            self.assertEqual(response.status_code,400,response.json)
            self.assertEqual(preferences(self.owner().id)['locale'],'zh-CN')
        self.assertEqual(self.client.post('/api/preferences/localization',json={'locale':'en'}).status_code,403)

    def test_localization_and_profiles_save_atomically(self):
        from backend.database.models import AppConfig
        from backend.services.discovery_profiles import user_profiles
        payload={'since_date':'2024-06-15','interval':30,'ai_mode':'rules','profiles':['graduate'],
                 'localization':{'education_system':'global','time_zone':'America/New_York'}}
        self.assertEqual(self.client.post('/api/collection/setup',json=dict(payload,interval=1),headers=self.headers).status_code,400)
        self.assertIsNone(user_profiles(self.owner().id)); self.assertIsNone(AppConfig.get('scrape_interval'))
        response=self.client.post('/api/collection/setup',json=payload,headers=self.headers)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(preferences(self.owner().id)['time_zone'],'America/New_York')
        self.assertEqual(preferences(self.owner().id)['education_system'],'global')
        self.assertEqual(preferences(self.owner().id)['locale'],'zh-CN')

    def test_school_country_and_date_order_are_not_inferred_from_english(self):
        from backend.database.db import db
        school=self.fixture.school
        stage_preferences(self.owner().id,{'locale':'en'}); db.session.commit()
        self.assertIsNone(school_context(school)['country_code'])
        self.assertEqual(school_context(school)['date_order'],'unknown')
        school.config={'localization':{'country_code':'gb','content_locale':'en-GB','date_order':'dmy','time_zone':'Europe/London'}}
        self.assertEqual(school_context(school),{'country_code':'GB','content_locale':'en-GB','date_order':'dmy','time_zone':'Europe/London'})

    def test_english_keeps_cleanup_action_ids_and_translates_validation_errors(self):
        from bs4 import BeautifulSoup
        before=BeautifulSoup(self.client.get('/admin/storage').data,'html.parser')
        modes=[node['data-cleanup'] for node in before.select('[data-cleanup]')]
        self.client.post('/api/preferences/localization',json={'locale':'en'},headers=self.headers)
        after=BeautifulSoup(self.client.get('/admin/storage').data,'html.parser')
        self.assertEqual(modes,[node['data-cleanup'] for node in after.select('[data-cleanup]')])
        self.assertTrue(after.select('[data-cleanup="discovery"]'))
        self.assertEqual(after.select_one('[data-storage-group="调查缓存"]').find_previous_sibling('th').get_text(strip=True),'Discovery cache')
        response=self.client.post('/api/collection/setup',json={'ai_mode':'rules','profiles':[]},headers=self.headers)
        self.assertEqual(response.status_code,400)
        self.assertTrue(response.json['error'].startswith('Select at least one'))

    def test_plural_interpolation_and_publication_wall_time(self):
        from datetime import datetime
        from flask import g
        from backend.services.localization import translate,date_label
        with self.fixture.fixture.app.test_request_context():
            g.ui_locale='en'
            self.assertEqual(translate('{count} 个栏目',count=1),'1 source')
            self.assertEqual(translate('{count} 个栏目',count=2),'2 sources')
            self.assertEqual(translate('已找到 1 条通知，后续自动更新'),'Found 1 notice. Updates continue automatically.')
            self.assertEqual(date_label(datetime(2026,9,30,9,10)),'30 Sep 2026 09:10')


class InternationalDiscoveryTermsTests(unittest.TestCase):
    def test_conditional_tooltips_cannot_hide_an_unregistered_branch(self):
        from scripts.checks.check_localization_catalog import unregistered_template_copy
        source = '''<button {% if url %}title="复制标题和官网原文链接"{% else %}
          data-i18n-title="暂无可分享的官网原文链接" title="{{ tr('暂无可分享的官网原文链接') }}"{% endif %}
          data-i18n="分享">{{ tr('分享') }}</button>'''
        self.assertEqual(unregistered_template_copy(source), ['Unregistered title: 复制标题和官网原文链接'])
        self.assertEqual(unregistered_template_copy(source.replace('title="复制标题',
            'data-i18n-title="复制标题和官网原文链接" title="复制标题')), [])

    def test_audit_catches_new_text_and_dynamic_accessible_labels(self):
        from scripts.checks.check_localization_catalog import unregistered_template_copy,unregistered_script_copy
        self.assertTrue(unregistered_template_copy('<p>尚未接入API</p>'))
        self.assertTrue(unregistered_script_copy("button.setAttribute('aria-label', '待复制的通知');"))
        self.assertTrue(unregistered_script_copy("button.textContent = busy ?\n '正在生成' : uiT('未完成');"))
        self.assertTrue(unregistered_script_copy("button.textContent = `已显示 ${count} 条`;"))
        self.assertEqual(unregistered_script_copy("button.innerHTML = `<span>${uiT('编辑')}</span>`;"), [])
        self.assertEqual(unregistered_script_copy("button.textContent = uiT(enabled ? '通过' : '未完成');"), [])
        self.assertEqual(unregistered_template_copy('<h1>{{ school.name }}</h1><p data-official-content>{{ article.body }}</p>'), [])

    def test_registered_ui_messages_have_complete_catalog_entries(self):
        from scripts.checks.check_localization_catalog import audit
        self.assertEqual(audit()['problems'],[])

    def test_english_terms_use_the_existing_need_identifiers(self):
        samples={'academic_units':'Schools and Departments','teaching':'Academic Calendar',
                 'admissions':'Postgraduate Admissions','funding':'Scholarships and Financial Aid',
                 'career':'Careers and Internships','competition_exchange':'Study Abroad',
                 'research':'Research Seminars','personnel':'Faculty Development','student_services':'Student Services'}
        for key,label in samples.items():
            with self.subTest(label=label):
                result=candidate_attention({'url':'https://example.edu/','label':label})
                self.assertIn(key,result['needs']); self.assertGreater(result['score'],0)
        self.assertTrue(academic_collection_label('Academic Departments'))
        self.assertTrue(academic_collection_label('Schools and Colleges'))
        self.assertFalse(academic_collection_label('School of Engineering'))

    def test_catalog_has_unique_keys_and_matching_placeholders(self):
        import re
        def unique(pairs):
            result={}
            for key,value in pairs:
                self.assertNotIn(key,result,'duplicate key: '+key); result[key]=value
            return result
        path=Path(__file__).resolve().parents[1]/'frontend/static/i18n/en.json'
        for source,target in json.loads(path.read_text('utf8'),object_pairs_hook=unique).items():
            for variant in (target.values() if isinstance(target,dict) else [target]):
                self.assertEqual(set(re.findall(r'\{(\w+)\}',source)),set(re.findall(r'\{(\w+)\}',variant)),source)
        self.assertEqual(english_catalog()['本科生'],'Undergraduate student')


if __name__=='__main__':unittest.main()
