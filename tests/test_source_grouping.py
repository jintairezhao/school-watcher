"""Missing placement is recovered from observed official paths, without AI."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import create_app
from backend.database.db import db
from backend.database.models import Department, School, Subscription, User
from backend.services.source_grouping import run_grouping

ROOT = 'https://example.edu.cn/'
ROSTER = ROOT + 'colleges/'
HOME = ROOT + 'earth/'
NORMAL = HOME + 'notices/'
CMS = ROOT + 'zcms/catalog/42/pc/index_1.shtml'


class GroupingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.app = create_app({'TESTING':True, 'SECRET_KEY':'grouping-test', 'SQLALCHEMY_DATABASE_URI':'sqlite://',
            'DISCOVERY_CACHE_PATH':str(folder / 'cache.db'), 'SOURCE_CATALOG_PATH':str(folder / 'catalog.db'),
            'SOURCE_EVIDENCE_PATH':str(folder / 'evidence')})
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.school = School(name='例校', url=ROOT, subscriber_count=1)
        self.user = User(username='reader', password_hash='unused')
        db.session.add_all([self.school,self.user]); db.session.flush()
        self.source = Department(school_id=self.school.id, name='地球学院', list_url=CMS,
            group_name='', list_selector='li.middleArticle--articleList')
        db.session.add(self.source); db.session.flush()
        self.subscription = Subscription(user_id=self.user.id, school_id=self.school.id, department_ids=[self.source.id])
        db.session.add(self.subscription); db.session.commit()
        articles = ''.join('<li class="middleArticle--articleList"><a class="middleArticle__articleList--article" '
            'href="' + HOME + 'c/2026-09-18/' + str(i) + '.shtml">学院学生学业奖学金评审事项' + str(i) + '</a>'
            '<span class="middleArticle__articleList--date">2026-09-18</span></li>' for i in range(3))
        listing = '<title>例校地球学院</title><div class="c-main__right"><div class="middleArticle__position">' + \
            '<div class="middleArticle__position--label"><a href="' + HOME + '">首页</a><a href="' + NORMAL + '">' + \
            '通知公告</a></div></div><div class="middleArticle__art"><ul class="middleArticle__articleList">' + \
            articles + '</ul></div><div class="middlePag"><script>return "' + ROOT + \
            'zcms/catalog/42/pc/index_{PageIndex}.shtml";</script></div></div>'
        self.pages = {ROOT:'<title>例校</title><nav><a href="' + ROSTER + '">院系设置</a></nav>',
            ROSTER:'<h1>院系设置</h1><table><tr><th>学院</th></tr><tr><td><a href="' + HOME + '">地球学院</a></td></tr></table>',
            HOME:'<title>例校地球学院</title><a href="' + NORMAL + '">通知公告</a>', NORMAL:listing, CMS:listing}

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.context.pop(); self.temp.cleanup()

    def test_slices_resume_and_restore_group_preserving_rules_and_selected_ids(self):
        fetched = []
        def fetch(url):
            fetched.append(url)
            return {'url':url, 'status':200, 'html':self.pages[url]}
        checkpoint = None
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Must not call AI')):
            for turn in range(5):
                result = run_grouping({'school_id':self.school.id, 'ai_assist':False},
                    checkpoint=checkpoint, fetcher=fetch, page_budget=2)
                checkpoint = result['checkpoint']
                if not result['continuation_required']:
                    break
        self.assertGreater(turn, 0)
        self.assertEqual(fetched, [ROOT, ROSTER, HOME, CMS, NORMAL])
        self.assertEqual(self.source.group_name, '院系设置')
        self.assertEqual(self.source.list_url, CMS)
        self.assertEqual(self.source.list_selector, 'li.middleArticle--articleList')
        self.assertEqual(self.subscription.department_ids, [self.source.id])
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(result['gaps'], [])

    def test_failed_official_directory_leaves_an_explicit_gap_and_no_guessed_group(self):
        def fetch(url):
            if url == ROSTER:
                return {'url':url, 'status':503, 'html':'', 'error':'official_directory_unavailable'}
            return {'url':url, 'status':200, 'html':self.pages[url]}
        result = run_grouping({'school_id':self.school.id, 'ai_assist':False}, fetcher=fetch, page_budget=20)
        self.assertFalse(result['continuation_required'])
        self.assertEqual(self.source.group_name, '')
        self.assertTrue(any(g.get('source_id') == self.source.id for g in result['gaps']))
        self.assertTrue(any(g['reason'] == 'official_directory_unavailable' for g in result['gaps']))

    def test_scheduler_places_recovery_in_directory_lane_without_model_assistance(self):
        from backend.worker import _schedule_due
        from backend.database.models import BackgroundTask
        school_id = self.school.id
        with patch('backend.services.directory_recovery.recover_legacy'):
            _schedule_due()
        task = BackgroundTask.query.filter_by(identity=f'source_grouping:{school_id}').one()
        self.assertEqual(task.capability, 'directory')
        self.assertFalse(task.payload['ai_assist'])

    def test_progress_keeps_finished_placement_gaps_visible_without_active_processing(self):
        from backend.database.models import BackgroundTask
        from backend.services.onboarding_progress import status
        db.session.add(BackgroundTask(identity=f'source_grouping:{self.school.id}', kind='source_grouping',
            payload={'school_id':self.school.id}, state='done', result={'gaps':[
                {'source_id':self.source.id,'url':self.source.list_url,'reason':'该旧入口归属仍待核实'}]}))
        db.session.commit()
        result = status(self.school)
        self.assertFalse(result['active'])
        self.assertEqual(result['processing_count'], 0)
        self.assertEqual(result['coverage']['placement_gap_count'], 1)
        self.assertIn('该旧入口归属仍待核实', result['incomplete_reasons'])

    def test_concurrent_manual_group_change_cannot_be_overwritten_by_late_recovery(self):
        source_id = self.source.id
        def placement(_):
            db.session.execute(db.update(Department).where(Department.id == source_id)
                .values(group_name='用户确认的分组').execution_options(synchronize_session=False))
            return {source_id:[{'group':'院系设置'}]}
        with patch('backend.services.source_placements.official_source_placements', side_effect=placement):
            result = run_grouping({'school_id':self.school.id,'ai_assist':False},
                fetcher=lambda url:{'url':url,'status':200,'html':self.pages[url]},page_budget=20)
        db.session.expire_all()
        self.assertEqual(db.session.get(Department, source_id).group_name, '用户确认的分组')
        self.assertNotIn(source_id, result['restored_ids'])

    def test_recovery_uses_official_groups_for_offices_research_and_admissions_without_name_rules(self):
        for group, unit in (('组织机构', '资产管理处'), ('科研机构', '先进材料研究中心'),
                            ('教学单位', '通识教育中心'), ('直属单位', '信息服务中心'),
                            ('教育教学', '通识教育中心'), ('科学研究', '先进材料研究中心'),
                            ('招生就业', '招生办公室')):
            with self.subTest(group=group):
                pages = {url:html.replace('院系设置', group).replace('地球学院', unit)
                         for url, html in self.pages.items()}
                self.source.name = unit; self.source.group_name = ''; db.session.commit()
                with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Must not call AI')):
                    result = run_grouping({'school_id':self.school.id, 'ai_assist':False},
                        fetcher=lambda url:{'url':url, 'status':200, 'html':pages[url]}, page_budget=20)
                self.assertEqual(self.source.group_name, group)
                self.assertEqual(result['gaps'], [])
                self.assertEqual(self.subscription.department_ids, [self.source.id])
