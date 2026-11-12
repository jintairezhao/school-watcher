"""Retained official list identity and interrupted rule-plan compatibility."""
from copy import deepcopy
from pathlib import Path
import gzip
import hashlib
import json
import unittest

from bs4 import BeautifulSoup
from backend.scraper.discovery.publication_lists import publication_lists

FIXTURES = Path(__file__).parent / 'fixtures/structure'
URL = 'http://www.swpu.edu.cn/dky/tzgg.htm'


def original():
    raw = gzip.decompress((FIXTURES / 'swpu-dky-notices-9143f897-20261102.html.gz').read_bytes())
    assert hashlib.sha256(raw).hexdigest() == '9143f897745554166828b27e2f7a0ece2724149d9f44cc7db724b0d85b308936'
    return raw.decode('utf-8')


def retained():
    return json.loads((FIXTURES / 'swpu-dky-pending-trial-20261102.json').read_text(encoding='utf-8'))['column_validation']


class LocalNoticeBreadcrumbTests(unittest.TestCase):
    def test_real_notice_uses_terminal_self_breadcrumb_not_template_heading(self):
        feeds = publication_lists(original(), URL)
        self.assertEqual(len(feeds), 1)
        feed = feeds[0]
        self.assertEqual(feed['name'], '通知公告')
        self.assertEqual(feed['column_url'], URL)
        self.assertEqual(feed['item_count'], 19)
        crumb = BeautifulSoup(original(), 'lxml').select(feed['heading_locator'])
        self.assertEqual(len(crumb), 1)
        self.assertEqual(crumb[0].get_text(' ', strip=True), '通知公告')
        self.assertEqual(feed['article_urls'], retained()['trial_columns'][0]['article_urls'])

    def test_other_page_crumb_does_not_fall_back_to_template_name(self):
        soup = BeautifulSoup(original(), 'lxml')
        soup.select_one('.right > .local').select('a')[-1]['href'] = 'other.htm'
        feeds = publication_lists(str(soup), URL)
        self.assertTrue(feeds)
        self.assertTrue(all(not f['name'] and f.get('heading_ambiguous') for f in feeds))

    def test_missing_local_crumb_does_not_borrow_sidebar_title(self):
        soup = BeautifulSoup(original(), 'lxml')
        soup.select_one('.right > .local').decompose()
        feeds = publication_lists(str(soup), URL)
        self.assertTrue(all(not f['name'] and f.get('heading_ambiguous') for f in feeds))

    def test_second_widget_cannot_borrow_the_page_breadcrumb(self):
        soup = BeautifulSoup(original(), 'lxml')
        listing = soup.select_one('.right > .news-text ul.list-ul')
        listing.parent.append(deepcopy(listing))
        feeds = publication_lists(str(soup), URL)
        self.assertTrue(feeds)
        self.assertTrue(all(not f['name'] and f.get('heading_ambiguous') for f in feeds))

    def test_pending_rule_plan_refreshes_only_identity_on_same_scope(self):
        from backend.services.source_onboarding import _refresh_unexecuted_rule_headings
        before = retained()
        frozen = deepcopy(before)
        after, count = _refresh_unexecuted_rule_headings(before, html=original(), url=URL)
        self.assertEqual(count, 1)
        self.assertEqual(before, frozen)
        old, new = before['trial_columns'][0], after['trial_columns'][0]
        self.assertEqual(new['name'], '通知公告')
        for key in ('list_selector','content_selector','title_selector','link_selector','date_selector',
                    'container_locator','article_urls','samples'):
            self.assertEqual(new[key], old[key])
        for key in ('trial_decision_ids','trial_request_key','completed','failures','department_ids','progress'):
            self.assertEqual(after[key], before[key])
        repeated, again = _refresh_unexecuted_rule_headings(after, html=original(), url=URL)
        self.assertEqual((repeated, again), (after, 0))

    def test_paid_finished_or_partly_executed_plan_is_not_rewritten(self):
        from backend.services.source_onboarding import _refresh_unexecuted_rule_headings
        for field, value in [('stage','done'), ('trial_origin_stage','ai'), ('trial_decision_ids',['paid:1']),
                             ('trial_request_key','paid-input'), ('completed',['prior']),
                             ('department_ids',[99]), ('failures',[{'issue':{'codes':['list_missing']}}])]:
            with self.subTest(field=field):
                old = retained()
                old[field] = value
                after, count = _refresh_unexecuted_rule_headings(old, html=original(), url=URL)
                self.assertEqual((after, count), (old, 0))

    def test_different_member_or_page_material_is_not_migrated(self):
        from backend.services.source_onboarding import _refresh_unexecuted_rule_headings
        old = retained()
        old['trial_columns'][0]['article_urls'] = old['trial_columns'][0]['article_urls'][1:]
        self.assertEqual(_refresh_unexecuted_rule_headings(old, html=original(), url=URL), (old, 0))
        old = retained()
        self.assertEqual(_refresh_unexecuted_rule_headings(old, html=original()+'changed', url=URL), (old, 0))
        old = retained()
        old['trial_columns'][0]['column_url'] = 'http://www.swpu.edu.cn/dky/other.htm'
        self.assertEqual(_refresh_unexecuted_rule_headings(old, html=original(), url=URL), (old, 0))


class LocalNoticeOnboardingTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_canonical_handoff_keeps_corrected_scope_and_requests_actual_next_page(self):
        from unittest.mock import patch
        from backend.database.db import db
        from backend.database.models import BackgroundTask, Department, School
        from backend.database.source_governance_models import SourceValidation
        from backend.services import tasks
        from backend.services.source_governance import _snapshot
        from backend.services.source_onboarding import onboard_page
        school = db.session.get(School, self.fixture.school_id)
        school.url = 'https://www.swpu.edu.cn/'
        db.session.commit()
        reference = _snapshot(original(), URL, role='list')
        task = tasks.enqueue('onboard', 'retained-notice', dict(school_id=school.id,url=URL,
            group_name='地球科学与技术学院',label='全部',ai_assist=True,snapshot=reference))
        task_id = task.id
        task.checkpoint = dict(onboarding_page=reference,column_validation=retained())
        db.session.commit()
        handle = tasks.claim(capabilities=['http'])
        self.assertEqual(handle['id'], task_id)
        requested = []

        def defer_before_http(url, purpose):
            requested.append((url,purpose))
            tasks.defer(phase='origin_wait',delay=1,error_code='origin_busy',
                checkpoint=dict(tasks.current_execution()['checkpoint']))

        deny = AssertionError('This test cannot fetch pages or invoke models')
        with patch('backend.ai.runtime.run_skill',side_effect=deny), \
             patch('requests.sessions.Session.send',side_effect=deny), \
             patch('socket.getaddrinfo',side_effect=deny):
            try:
                with tasks.execution_scope(handle):
                    onboard_page(handle['payload'],fetcher=defer_before_http)
            except tasks.TaskDeferred as deferred:
                tasks.handoff(handle,deferred)
            else:
                self.fail('The missing next-page material must remain pending')
        db.session.expire_all()
        task = db.session.get(BackgroundTask,task_id)
        self.assertEqual((task.state,task.phase,task.generation),('pending','origin_wait',1))
        self.assertEqual(task.checkpoint['column_validation']['trial_columns'][0]['name'],'通知公告')
        self.assertEqual(requested, [('http://www.swpu.edu.cn/dky/tzgg/29.htm','list')])
        self.assertEqual(Department.query.count(),0)
        self.assertEqual(SourceValidation.query.count(),0)


if __name__ == '__main__':
    unittest.main()
