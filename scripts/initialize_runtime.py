"""One-shot schema/seed initializer used before starting any server processes."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from migrate_safely import migrate

if __name__ == '__main__':
    migrate()
    from backend import create_app
    from backend.core.config import load_config_yaml
    from backend.database.db import db
    with create_app({'TESTING': True}).app_context():
        load_config_yaml()
        db.session.remove()
        db.engine.dispose()
