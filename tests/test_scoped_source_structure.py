"""Bounded source evidence refresh must preserve unrelated structural authority."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_school_structure as fixture
from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry
from backend.services.school_structure import sync_official_structure, FACT_WRITER
from backend.services.structure_versions import current_version


class ScopedSourceStructureTests(unittest.TestCase):
    setUp = fixture.SchoolStructureTests.setUp
    tearDown = fixture.SchoolStructureTests.tearDown
    publish = fixture.SchoolStructureTests.publish
    connect = fixture.SchoolStructureTests.connect

    def prepare(self):
        self.publish()
        target = self.connect(fixture.CS + 'notices/')['department_ids'][0]
        other = self.connect(fixture.LANG + 'notices/')['department_ids'][0]
        unrelated = DepartmentDirectoryEntry.query.filter_by(department_id=other).first()
        self.assertIsNotNone(unrelated)
        # A previously retained context owned by the canonical writer would be
        # rederived by an unrestricted sync. This operation has no authority to
        # replace it while refreshing the other source.
        unrelated.facts_json = [*deepcopy(unrelated.facts_json),
            dict(writer=FACT_WRITER, type='directory_listing',
                 observed_relation='prior_unrelated_context', evidence=[])]
        db.session.commit()
        return target, other, deepcopy(unrelated.facts_json)

    def test_target_refresh_preserves_other_source_and_all_organization_rows(self):
        target, other, original = self.prepare()
        rows = {row.id: deepcopy(row.identity_json) for row in Department.query.all()}
        result = sync_official_structure(self.school, self.catalog, only_source_ids={target})
        self.assertEqual(result['scoped_source_ids'], [target])
        self.assertEqual({row.id: row.identity_json for row in Department.query.all()}, rows)
        self.assertEqual(DepartmentDirectoryEntry.query.filter_by(department_id=other).first().facts_json, original)
        self.assertTrue(DepartmentDirectoryEntry.query.filter_by(department_id=target).count())

    def test_invalid_source_scope_cannot_fall_through_to_full_sync(self):
        target, other, original = self.prepare()
        version = current_version(self.school.id)
        unit = Department.query.filter_by(kind='unit').first()
        for invalid in (set(), {True}, {target, 999999}, {unit.id}):
            with self.subTest(scope=invalid), self.assertRaises(ValueError):
                sync_official_structure(self.school, self.catalog, only_source_ids=invalid)
            db.session.rollback()
            self.assertEqual(current_version(self.school.id), version)
            self.assertEqual(DepartmentDirectoryEntry.query.filter_by(department_id=other).first().facts_json, original)

    def test_repeat_is_idempotent_and_failed_commit_keeps_prior_target_context(self):
        target, other, original = self.prepare()
        sync_official_structure(self.school, self.catalog, only_source_ids={target})
        version = current_version(self.school.id)
        sync_official_structure(self.school, self.catalog, only_source_ids={target})
        self.assertEqual(current_version(self.school.id), version)
        link = DepartmentDirectoryEntry.query.filter_by(department_id=target).first()
        link.facts_json = [*deepcopy(link.facts_json),
            dict(writer=FACT_WRITER, type='directory_listing', observed_relation='prior_target_context', evidence=[])]
        db.session.commit()
        previous, version = deepcopy(link.facts_json), current_version(self.school.id)
        with patch.object(db.session, 'commit', side_effect=ValueError('injected failed commit')):
            with self.assertRaisesRegex(ValueError, 'injected failed commit'):
                sync_official_structure(self.school, self.catalog, only_source_ids={target})
        db.session.rollback()
        self.assertEqual(current_version(self.school.id), version)
        self.assertEqual(DepartmentDirectoryEntry.query.filter_by(department_id=target).first().facts_json, previous)
        self.assertEqual(DepartmentDirectoryEntry.query.filter_by(department_id=other).first().facts_json, original)


if __name__ == '__main__':
    unittest.main()
