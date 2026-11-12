"""Reader failures explain the recovery without exposing extraction settings."""
import unittest
from backend.database.db import db
from backend.database.models import Announcement, Department


class ReaderFailureCopyTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.f = DirectOnboardingTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)
        source = Department(school_id=self.f.school_id, name='通知公告', list_url='https://example.edu.cn/news/', list_selector='li')
        db.session.add(source); db.session.flush()
        self.ann = Announcement(school_id=source.school_id, department_id=source.id,
            title='测试通知', url='https://example.edu.cn/news/1.htm')
        db.session.add(self.ann); db.session.commit()
        self.client = self.f.app.test_client()

    def test_old_internal_failure_has_a_readable_message_and_keeps_original_evidence(self):
        original = '未找到配置要求的目标内容，需要核对来源规则'
        self.ann.content_error = original; db.session.commit()
        data = self.client.get(f'/api/announcements/{self.ann.id}/content').get_json()
        self.assertEqual(data['content_status'], 'failed')
        self.assertEqual(data['error'], '暂时没能提取这篇文章的正文，可以重新加载，或打开官网原文阅读。')
        self.assertEqual(self.ann.content_error, original)

    def test_sign_in_failure_is_not_replaced_by_extraction_failure(self):
        original = '这篇通知需要登录学校网站后查看，请打开官网原文。'
        self.ann.content_error = original; db.session.commit()
        data = self.client.get(f'/api/announcements/{self.ann.id}/content').get_json()
        self.assertEqual(data['error'], original)

    def test_extraction_failure_is_translated(self):
        self.ann.content_error = '来源的内容识别规则需要核对'; db.session.commit()
        self.client.set_cookie('watcher_locale', 'en')
        data = self.client.get(f'/api/announcements/{self.ann.id}/content').get_json()
        from shared.ui_messages import translate_for_locale
        self.assertEqual(translate_for_locale(data['error'], 'en'),
            'The article text could not be extracted. Reload it, or read the original on the website.')


if __name__ == '__main__': unittest.main()
