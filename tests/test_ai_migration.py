"""Real SQLite upgrade/restore drills for shared AI data; isolated files only."""
from datetime import datetime
import os
import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from flask_migrate import upgrade
import sqlalchemy as sa
from backend import create_app
from backend.core.config import ROOT_DIR, ensure_field_encryption_key
from backend.database.db import db
from backend.ai.models import AIProfile, AIExecution, AIBudget
from backend.ai.summary_models import AnnouncementSummary
from backend.database.models import AppConfig
from backend.services.backups import create_backup, restore_backup


class AIMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='watcher-ai-migrate-');self.root=Path(self.tmp.name)
        self.apps=[];self.env=patch.dict(os.environ,{'SECRET_KEY':'isolated-test'});self.env.start()

    def tearDown(self):
        for app in self.apps:
            with app.app_context():db.session.remove();db.engine.dispose()
        self.env.stop();self.tmp.cleanup()

    def app(self,path=None):
        app=create_app({'TESTING':True,'SECRET_KEY':'isolated-test',
            'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(path or self.root/'main.db'),
            'SOURCE_CATALOG_PATH':str(self.root/'catalog.sqlite3'),'BACKUP_DIR':str(self.root/'backups'),'BACKUP_COPY_DIR':''})
        self.apps.append(app);return app

    def test_old_summary_preserved_without_fabricated_version(self):
        app=self.app()
        with app.app_context():
            upgrade(directory=str(ROOT_DIR/'migrations'),revision='23b7da405f91')
            with db.engine.begin() as connection:
                connection.execute(sa.text("INSERT INTO schools (id,name,url,subscriber_count) VALUES (1,'school','https://example.edu',0)"))
                connection.execute(sa.text("INSERT INTO departments (id,school_id,name) VALUES (1,1,'college')"))
                connection.execute(sa.text("INSERT INTO announcements (id,school_id,department_id,title,summary) VALUES (1,1,1,'old','历史摘要')"))
            upgrade(directory=str(ROOT_DIR/'migrations'))
            summary=AnnouncementSummary.query.one()
            self.assertEqual(summary.state,'historical');self.assertEqual(summary.summary,'历史摘要')
            self.assertEqual(summary.provenance,{'origin':'legacy','version_known':False})
            self.assertEqual(summary.body_snapshot,'');self.assertEqual(summary.binding,{})
            self.assertEqual(db.session.execute(sa.text('SELECT summary FROM announcements')).scalar(),'历史摘要')
            self.assertEqual(set(sa.inspect(db.engine).get_table_names())-set(db.metadata.tables),{'alembic_version'})

    def test_restore_fences_uncertain_calls_retains_reservation(self):
        app=self.app()
        with app.app_context():
            upgrade(directory=str(ROOT_DIR/'migrations'))
            profile=AIProfile(name='test',provider='deepseek',model='fixture',region='default',
                              encrypted_key='enc:fixture',version=1,tested_version=1,enabled=True)
            db.session.add(profile);db.session.flush()
            db.session.add(AIBudget(key='total:2026-09',reserved_tokens=100,used_tokens=20,active_count=1))
            db.session.add(AIExecution(execution_id='paid-in-flight',purpose='summary',profile_id=profile.id,
                config_version='1',provider='deepseek',model='fixture',skill_id='summary',skill_version='1',
                skill_digest='d'*64,input_digest='e'*64,mode='summary',status='sending',reserved_tokens=100,
                budget_keys=['total:2026-09']))
            db.session.commit()
            evidence_body='<html>official evidence</html>'.encode()
            evidence_hash=hashlib.sha256(evidence_body).hexdigest()
            evidence_root=self.root/'source-governance-evidence';evidence_root.mkdir()
            self.app_evidence=evidence_root/(evidence_hash+'.html.gz')
            self.app_evidence.write_bytes(gzip.compress(evidence_body))
            app.config['SOURCE_GOVERNANCE_EVIDENCE_PATH']=str(evidence_root)
            backup=create_backup()
            from backend.services.data_transfer import read_backup
            with (self.root/'backups'/backup['backup']).open('rb') as stream:
                portable=read_backup(stream)
            self.assertEqual(portable['format'],'school-watcher-collected-data')
        destination=self.root/'restored'
        restore_backup(self.root/'backups'/backup['backup'],destination)
        restored_evidence=destination/'source-governance-evidence'/self.app_evidence.name
        self.assertEqual(gzip.decompress(restored_evidence.read_bytes()),evidence_body)
        restored=self.app(destination/'main.db')
        with restored.app_context():
            self.assertEqual(AIExecution.query.one().status,'uncertain')
            self.assertEqual(AIBudget.query.one().reserved_tokens,100)
            self.assertEqual(AIBudget.query.one().active_count,0)
            self.assertFalse(AIProfile.query.one().enabled)
            self.assertEqual(AppConfig.get('ai_restore_review_required'),'1')

    def test_profile_deletion_upgrade_preserves_existing_service(self):
        app = self.app()
        with app.app_context():
            upgrade(directory=str(ROOT_DIR / 'migrations'), revision='45d9fc6271b3')
            profiles = sa.Table('ai_profiles', sa.MetaData(), autoload_with=db.engine)
            with db.engine.begin() as connection:
                connection.execute(profiles.insert().values(name='saved service', provider='deepseek',
                    model='fixture', region='default', encrypted_key='enc:fixture', version=3,
                    tested_version=3, enabled=True, max_output_tokens=4096, last_test_code='ok',
                    created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
            upgrade(directory=str(ROOT_DIR / 'migrations'))
            profile = AIProfile.query.one()
            self.assertIsNone(profile.deleted_at)
            self.assertTrue(profile.enabled)
            self.assertEqual(profile.encrypted_key, 'enc:fixture')
            self.assertEqual(profile.tested_version, 3)

    def test_local_master_key_persists_and_production_requires_configuration(self):
        folder=self.root/'data';folder.mkdir()
        with patch('backend.core.config.DATA_DIR',folder), patch.dict(os.environ,{'FIELD_ENC_KEY':'','WATCHER_ENV':''}):
            ensure_field_encryption_key();first=os.environ['FIELD_ENC_KEY']
            os.environ['FIELD_ENC_KEY']='';ensure_field_encryption_key()
            self.assertEqual(os.environ['FIELD_ENC_KEY'],first)
            self.assertGreaterEqual(len(first),32)
        with patch.dict(os.environ,{'FIELD_ENC_KEY':'','WATCHER_ENV':'production'}):
            with self.assertRaises(RuntimeError):ensure_field_encryption_key()


    def test_admin_ledger_and_reconciliation_do_not_expose_output(self):
        from backend.database.models import User
        app=self.app()
        with app.app_context():
            db.create_all()
            admin=User(username='admin',password_hash='fixture',role='admin')
            reader=User(username='reader',password_hash='fixture',role='user')
            db.session.add_all([admin,reader]);db.session.commit()
            admin_id,reader_id=admin.id,reader.id
            db.session.add(AIBudget(key='total:month',reserved_tokens=100,active_count=0))
            db.session.add(AIExecution(execution_id='unknown-call',purpose='summary',config_version='1',
                provider='deepseek',model='fixture',skill_id='summary',skill_version='1',mode='summary',
                skill_digest='a'*64,input_digest='b'*64,status='uncertain',output={'private':'never-expose'},
                reserved_tokens=100,budget_keys=['total:month'],active_released=True))
            db.session.commit()
        def client(ident):
            client=app.test_client()
            with client.session_transaction() as session:session.update(user_id=ident,_csrf_token='token')
            return client
        ordinary=client(reader_id)
        self.assertEqual(ordinary.get('/api/admin/ai/executions').status_code,403)
        admin=client(admin_id)
        response=admin.get('/api/admin/ai/executions')
        self.assertEqual(response.status_code,200);self.assertNotIn('never-expose',response.get_data(as_text=True))
        response=admin.post('/api/admin/ai/executions/reconcile',json={'execution_id':'unknown-call','total_tokens':15},headers={'X-CSRF-Token':'token'})
        self.assertEqual(response.status_code,200);self.assertNotIn('never-expose',response.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(AIBudget.query.one().used_tokens,15)
            self.assertEqual(AIBudget.query.one().reserved_tokens,0)
