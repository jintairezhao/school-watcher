"""Catalog identity is shared across subscribers, not inferred from a host."""
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import School
from backend.database.school_registry_models import SchoolRegistryEntry
from backend.services.school_registry import ensure_school, rename_school, stable_registry_key, registered_school


class SchoolRegistryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'data' / 'test-runtime'
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root)
        self.app = create_app({'TESTING': True, 'SQLALCHEMY_DATABASE_URI':
                              'sqlite:///' + (Path(self.tmp.name) / 'registry.sqlite3').as_posix()})
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.tmp.cleanup()

    def test_one_hundred_concurrent_registrations_share_identity(self):
        def run(_):
            with self.app.app_context():
                row, _ = ensure_school('示例大学', 'https://example.edu.cn/')
                ident = row.id
                db.session.remove()
                return ident
        with ThreadPoolExecutor(max_workers=12) as pool:
            ids = list(pool.map(run, range(100)))
        self.assertEqual(1, len(set(ids)))
        with self.app.app_context():
            self.assertEqual(1, School.query.count())
            self.assertEqual(1, SchoolRegistryEntry.query.count())

    def test_shared_domain_does_not_merge_campuses(self):
        with self.app.app_context():
            a, _ = ensure_school('示例大学', 'https://example.edu.cn/')
            aid = a.id
            b, _ = ensure_school('示例大学（校区）', 'https://example.edu.cn/campus/')
            self.assertNotEqual(aid, b.id)

    def test_existing_name_alias_keeps_old_school_id(self):
        with self.app.app_context():
            old = School(name='中国石油大学(北京)', url='http://www.cup.edu.cn/')
            db.session.add(old); db.session.commit()
            ident = old.id
            found, created = ensure_school('中国石油大学（北京）', 'https://www.cup.edu.cn/')
            self.assertFalse(created)
            self.assertEqual(ident, found.id)
            self.assertEqual('http://www.cup.edu.cn/', found.url)


    def test_administrator_rename_keeps_aliases_and_frozen_scope_identity(self):
        from backend.services.onboarding_acceptance import freeze_scope, acceptance_report
        entries=[{'name':'原名大学','url':'https://example.edu.cn/'}]
        with self.app.app_context(), patch('backend.services.onboarding_acceptance.catalog_entries',return_value=entries):
            school,_=ensure_school('原名大学',entries[0]['url'])
            ident=school.id;key=stable_registry_key(school);snapshot=freeze_scope()
            rename_school(school,'新名大学');db.session.commit()
            self.assertEqual(stable_registry_key(school),key)
            self.assertEqual(registered_school('原名大学').id,ident)
            self.assertEqual(registered_school('新名大学').id,ident)
            for name in ['原名大学','新名大学']:
                bound,created=ensure_school(name,entries[0]['url'])
                self.assertEqual(bound.id,ident);self.assertFalse(created)
            self.assertEqual(School.query.count(),1)
            self.assertEqual(SchoolRegistryEntry.query.count(),1)
            report=acceptance_report(snapshot)
            self.assertEqual(report['total_schools'],1)
            self.assertNotEqual(report['schools'][0]['state'],'not_registered')
            self.assertEqual(freeze_scope()['scope_digest'],snapshot['scope_digest'])

    def test_renaming_to_existing_alias_is_rejected(self):
        with self.app.app_context():
            school,_=ensure_school('学校甲','https://a.edu.cn/')
            other,_=ensure_school('学校乙','https://b.edu.cn/')
            ident=school.id
            with self.assertRaisesRegex(ValueError,'使用'):
                rename_school(school,'学校乙')
            db.session.rollback()
            self.assertEqual(db.session.get(School,ident).name,'学校甲')

    def test_duplicate_legacy_identity_needs_review_without_merging(self):
        with self.app.app_context():
            db.session.add_all([School(name='例校', url='https://one.edu.cn/'),
                                School(name='例校', url='https://two.edu.cn/')])
            db.session.commit()
            with self.assertRaisesRegex(ValueError, '重复'):
                ensure_school('例校', 'https://one.edu.cn/')
            self.assertEqual(2, School.query.count())


if __name__ == '__main__':
    unittest.main()
