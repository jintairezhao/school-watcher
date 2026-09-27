import hashlib
import tempfile
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, Department, School
from backend.services.source_catalog import FIELDS
from scripts.sources.apply_source_review import apply_review


class ApplySourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(self.folder/'test.db')})
        self.context = self.app.app_context(); self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(db.engine.dispose)
        self.addCleanup(db.session.remove)
        db.create_all()
        school = School(id=1, name='示例大学', url='https://example.edu.cn/')
        row = Department(id=1, school_id=1, name='Read', list_url=school.url, list_selector='li', group_name='')
        db.session.add_all([school, row]); db.session.flush()
        db.session.add(Announcement(id=1, school_id=1, department_id=1, title='历史通知', url=school.url+'old'))
        db.session.commit()
        html = '<ul><li><a href="/post/1">推免生综合考核通知</a></li></ul>'
        (self.folder/'page.html').write_text(html, encoding='utf-8')
        self.old = {key: getattr(row, key) for key in ('name', *FIELDS)}
        self.new = dict(self.old, name='招生通知', title_selector='a', link_selector='a',
                        date_selector='', content_selector='', evidence_file='page.html',
                        evidence_sha256=hashlib.sha256(html.encode()).hexdigest())
        self.review = {'school_id': 1, 'school_url': school.url,
                       'updates': [{'id': 1, 'expected': self.old, 'config': self.new}], 'additions': []}

    def test_dry_run_and_repeated_application_preserve_source_and_history(self):
        from backend.database.source_governance_models import SourceProposal
        self.assertFalse(apply_review(self.review, self.folder)['applied'])
        self.assertEqual(db.session.get(Department, 1).name, 'Read')
        self.assertEqual(apply_review(self.review, self.folder, apply=True)['updates'], 1)
        self.assertEqual(db.session.get(Department, 1).name, 'Read')
        self.assertEqual(db.session.get(Announcement, 1).department_id, 1)
        second = apply_review(self.review, self.folder, apply=True)
        self.assertEqual(second['activated'], 0)
        self.assertEqual(SourceProposal.query.count(), 1)
        self.assertEqual(SourceProposal.query.one().department_id, 1)

    def test_stale_review_cannot_overwrite_another_edit(self):
        db.session.get(Department, 1).name = '管理员新名称'; db.session.commit()
        with self.assertRaisesRegex(ValueError, '已被修改'):
            apply_review(self.review, self.folder, apply=True)
        self.assertEqual(db.session.get(Department, 1).name, '管理员新名称')

    def test_changed_evidence_rejects_entire_review(self):
        (self.folder/'page.html').write_text('<p>访问验证</p>', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已改变'):
            apply_review(self.review, self.folder, apply=True)
        self.assertEqual(db.session.get(Department, 1).name, 'Read')


if __name__ == '__main__':
    unittest.main()
