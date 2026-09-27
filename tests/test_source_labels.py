"""Evidence-backed captions agree with the tree, without trusting HTML labels."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import render_template_string

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import create_app
from backend.database.db import db
from backend.database.models import Department, School
from backend.services.source_labels import source_breadcrumb


class SourceLabelTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'source-label-test',
                              'SQLALCHEMY_DATABASE_URI': 'sqlite://',
                              'SOURCE_CATALOG_PATH': ''})
        self.source = SimpleNamespace(id=1, name='通知公告', group_name='地球学院',
                                      school_id=1, list_url='https://example.edu.cn/earth/notices/')

    def test_without_evidence_preserves_original_group_and_name(self):
        self.assertEqual(source_breadcrumb(self.source), '地球学院 / 通知公告')
        self.source.group_name = None
        self.assertEqual(source_breadcrumb(self.source), '通知公告')
        self.assertEqual(source_breadcrumb(None), '')

    def test_official_units_are_included_and_the_requested_group_is_preferred(self):
        placements = [dict(group=group, nodes=[{'name': '地球学院'}], label='通知公告')
                      for group in ('院系设置', '科学研究')]
        with patch('backend.services.source_labels.official_source_placements', return_value={1: placements}):
            self.assertEqual(source_breadcrumb(self.source), '院系设置 / 地球学院 / 通知公告')
            with self.app.test_request_context('/?group=科学研究'):
                self.assertEqual(source_breadcrumb(self.source), '科学研究 / 地球学院 / 通知公告')
            with self.app.test_request_context('/?group=未知分类'):
                self.assertEqual(source_breadcrumb(self.source), '院系设置 / 地球学院 / 通知公告')
        self.assertEqual(self.source.group_name, '地球学院')

    def test_registered_filter_escapes_fallback_and_official_html(self):
        self.source.name = '<script>alert(1)</script>'
        with self.app.test_request_context('/'):
            rendered = render_template_string('{{ item|source_breadcrumb }}', item=self.source)
            self.assertNotIn('<script>', rendered)
            self.assertIn('&lt;script&gt;', rendered)
            placement = {'group': '<b>院系</b>', 'nodes': [{'name': '<img onerror=1>'}],
                         'label': '<script>alert(1)</script>'}
            with patch('backend.services.source_labels.official_source_placements', return_value={1: [placement]}):
                rendered = render_template_string('{{ item|source_breadcrumb }}', item=self.source)
                self.assertNotIn('<img', rendered)
                self.assertIn('&lt;b&gt;院系&lt;/b&gt;', rendered)
                self.assertIn('&lt;img onerror=1&gt;', rendered)

    def test_same_school_captions_share_request_catalogue_cache(self):
        self.app.config['SOURCE_CATALOG_PATH'] = 'unused-label-test-catalog.db'
        with self.app.app_context():
            db.create_all()
            try:
                school = School(name='例校', url='https://example.edu.cn/')
                db.session.add(school); db.session.flush()
                items = [Department(school_id=school.id, name=name, group_name='院系设置',
                                    list_url='https://example.edu.cn/earth/' + path)
                         for name, path in [('通知公告', 'notices/'), ('院内新闻', 'news/')]]
                db.session.add_all(items); db.session.commit()
                nodes = [{'kind': 'group', 'name': '院系设置', 'relation': 'page_identity'},
                         {'kind': 'unit', 'name': '地球学院', 'url': 'https://example.edu.cn/earth/'}]
                paths = {item.list_url: [{'basis': 'official_website_entry', 'nodes': nodes}]
                         for item in items}
                with patch('backend.services.source_placements.RuntimeCatalog.paths', return_value=paths) as read:
                    with self.app.test_request_context('/'):
                        self.assertEqual(source_breadcrumb(items[0]), '院系设置 / 地球学院 / 通知公告')
                        self.assertEqual(source_breadcrumb(items[1]), '院系设置 / 地球学院 / 院内新闻')
                        source_breadcrumb(items[0])
                        self.assertEqual(read.call_count, 1)
                    with self.app.test_request_context('/'):
                        source_breadcrumb(items[0])
                        self.assertEqual(read.call_count, 2)
            finally:
                db.session.remove(); db.engine.dispose()


if __name__ == '__main__':
    unittest.main()
