"""Valid existing CSS must survive export and pending-review import unchanged."""
from pathlib import Path
import json
import unittest

from backend.database.db import db
from backend.database.models import Department
from backend.database.source_governance_models import SourceProposal,SourceTransferIdentity
from backend.services.data_transfer import export_data,read_backup,merge_data,validate_data
from backend.services.source_governance import _candidate,source_config
from backend.services.source_agent import validation_feedback
import test_storage_management as fixture

LIMIT=4096
def selector(length):return 'div.'+'x'*(length-7)+' li'


class LongSourceSelectorTests(unittest.TestCase):
    setUp=fixture.StorageManagementTests.setUp
    tearDown=fixture.StorageManagementTests.tearDown
    client_as=fixture.StorageManagementTests.client_as

    def test_current_length_rule_survives_backup_and_pending_review(self):
        original=selector(505);self.dept.list_selector=original;db.session.commit()
        key=SourceTransferIdentity.query.filter_by(source_id=self.dept.id,kind='record').one().transfer_key
        with export_data() as package:data=read_backup(package)
        row=next(r for r in data['departments'] if r['id']==self.dept.id)
        self.assertEqual(row['list_selector'],original);self.assertEqual(row['transfer_key'],key)
        db.session.remove();db.drop_all();db.create_all()
        result=merge_data(data)
        restored=Department.query.one()
        self.assertFalse(restored.list_selector,'Import still requires review instead of activating a source')
        self.assertEqual(json.loads(SourceProposal.query.one().candidate_json)['list_selector'],original)
        self.assertEqual(SourceTransferIdentity.query.filter_by(source_id=restored.id,kind='record').one().transfer_key,key)
        self.assertEqual(result['sources_pending_review'],1)

    def test_governance_and_feedback_preserve_valid_long_selectors(self):
        original=selector(505);config=dict(source_config(self.dept),list_selector=original)
        self.assertEqual(_candidate(config)['list_selector'],original)
        self.assertEqual(validation_feedback(config,[])['list_selector'],original)
        at_limit=dict(config,list_selector=selector(LIMIT))
        self.assertEqual(_candidate(at_limit)['list_selector'],at_limit['list_selector'])

    def test_oversized_rules_are_rejected_not_silently_shortened(self):
        config=dict(source_config(self.dept),list_selector=selector(LIMIT+1))
        with self.assertRaises(ValueError):_candidate(config)
        data=fixture.StorageManagementTests.sample(self)
        data['departments'][0]['list_selector']=config['list_selector']
        with self.assertRaises(ValueError):validate_data(data)
        self.assertEqual(data['departments'][0]['list_selector'],config['list_selector'])

    def test_models_and_ai_schema_use_the_same_selector_limit(self):
        fields=('list_selector','title_selector','link_selector','date_selector','content_selector')
        for field in fields:self.assertEqual(Department.__table__.columns[field].type.length,LIMIT)
        root=Path(__file__).resolve().parents[1]/'backend/ai/skills/university-source-onboarding'
        schema=json.loads((root/'column.schema.json').read_text(encoding='utf-8'))
        for field in fields:self.assertEqual(schema['properties']['columns']['items']['properties'][field]['maxLength'],LIMIT)
        def selectors(node):
            if isinstance(node,dict):
                for name,value in node.get('properties',{}).items():
                    if name.endswith('_selector'):yield value
                for value in node.values():yield from selectors(value)
            elif isinstance(node,list):
                for value in node:yield from selectors(value)
        for filename in ('column.schema.json','extraction.schema.json'):
            for spec in selectors(json.loads((root/filename).read_text(encoding='utf-8'))):
                self.assertEqual(spec['maxLength'],LIMIT,filename)

    def migration(self):
        import importlib.util
        path=Path(__file__).resolve().parents[1]/'migrations/versions/8d3f659abc18_long_source_selectors.py'
        spec=importlib.util.spec_from_file_location('long_selector_migration',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_sqlite_upgrade_preserves_legacy_declared_schema_and_values(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from unittest.mock import patch
        module=self.migration();engine=sa.create_engine('sqlite://')
        try:
            with engine.begin() as connection:
                connection.execute(sa.text('CREATE TABLE departments (id INTEGER PRIMARY KEY, '+
                    ', '.join(field+' VARCHAR(500)' for field in module.FIELDS)+')'))
                connection.execute(sa.text('INSERT INTO departments(id,list_selector) VALUES(1,:value)'),{'value':selector(505)})
                original=connection.execute(sa.text("SELECT sql FROM sqlite_master WHERE name='departments'")).scalar_one()
                with patch.object(module,'op',Operations(MigrationContext.configure(connection))):
                    module.upgrade()
                    with self.assertRaises(RuntimeError):module.downgrade()
                self.assertEqual(connection.execute(sa.text("SELECT sql FROM sqlite_master WHERE name='departments'")).scalar_one(),original)
                self.assertEqual(connection.execute(sa.text('SELECT list_selector FROM departments WHERE id=1')).scalar_one(),selector(505))
        finally:engine.dispose()

    def test_postgresql_upgrade_compiles_widening_without_data_rewrite(self):
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from unittest.mock import patch
        statements=[]
        engine=sa.create_mock_engine('postgresql://',lambda sql,*args,**kwargs:statements.append(str(sql.compile(dialect=engine.dialect))))
        module=self.migration()
        with patch.object(module,'op',Operations(MigrationContext.configure(engine.connect()))):module.upgrade()
        self.assertEqual(len(statements),5)
        self.assertTrue(all('TYPE VARCHAR(4096)' in statement for statement in statements))


if __name__=='__main__':unittest.main()
