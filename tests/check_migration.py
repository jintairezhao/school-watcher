"""Upgrade an old schema, retain subscriptions/reads and compare with current models."""
import sys
import tempfile
from pathlib import Path
from sqlalchemy import text
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import upgrade

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db


def check():
    with tempfile.TemporaryDirectory(prefix='watcher-migration-') as directory:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'migration-test',
                          'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(directory) / 'old.db')})
        with app.app_context():
            upgrade(directory=str(ROOT / 'migrations'), revision='d4693c99491f')
            db.session.execute(text("INSERT INTO schools (id,name,url,enabled,subscriber_count) VALUES (1,'Migration school','https://www.tsinghua.edu.cn',1,1)"))
            db.session.execute(text("INSERT INTO departments (id,school_id,name,group_name) VALUES (1,1,'Original source','Original group')"))
            db.session.execute(text("INSERT INTO announcements (id,school_id,department_id,title) VALUES (1,1,1,'Existing notice')"))
            db.session.execute(text('INSERT INTO subscriptions (user_id,school_id) VALUES (1,1)'))
            db.session.execute(text('INSERT INTO user_reads (user_id,announcement_id) VALUES (1,1)'))
            db.session.execute(text("INSERT INTO scrape_logs (id,school_id,started_at,finished_at,status,new_count,total_count,error_message) VALUES (42,1,'2026-09-20 12:00:00','2026-09-20 12:00:12','failed',3,12,'Existing failure detail')"))
            db.session.commit()
            upgrade(directory=str(ROOT / 'migrations'))
            assert db.session.execute(text('SELECT count(*) FROM user_reads')).scalar() == 1
            assert db.session.execute(text('SELECT department_ids FROM subscriptions')).scalar() is None
            assert db.session.execute(text('SELECT title FROM announcements')).scalar() == 'Existing notice'
            assert db.session.execute(text('SELECT announcement_id,department_id FROM announcement_sources')).one() == (1, 1)
            assert db.session.execute(text('SELECT id,school_id,status,new_count,total_count,error_message,source_name FROM scrape_logs')).one() == (42,1,'failed',3,12,'Existing failure detail','')
            with db.engine.connect() as connection:
                changes = compare_metadata(MigrationContext.configure(connection), db.metadata)
                assert not changes, changes
            print('Old schema migration passed; subscriptions, reads and source metadata retained; model schema matches.')
            db.session.remove()
            db.engine.dispose()


if __name__ == '__main__':
    check()
