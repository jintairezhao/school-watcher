"""A nullable identity field preserves the preceding schema's existing rows."""
import importlib.util
from pathlib import Path
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


class OrganizationIdentityMigrationTests(unittest.TestCase):
    def test_existing_department_values_survive_additive_migration(self):
        path = Path(__file__).parents[1] / 'migrations/versions/5a0c32678fe5_organization_identity_evidence.py'
        spec = importlib.util.spec_from_file_location('organization_identity_migration', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine('sqlite://')
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE departments(id INTEGER PRIMARY KEY, name TEXT, list_url TEXT)')
                connection.exec_driver_sql("INSERT INTO departments VALUES(7, '原单位', 'https://example.edu.cn/')")
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
                row = connection.exec_driver_sql('SELECT id,name,list_url,identity_json FROM departments').one()
                self.assertEqual(tuple(row), (7, '原单位', 'https://example.edu.cn/', None))
        finally:
            engine.dispose()
