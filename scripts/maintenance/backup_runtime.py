"""Create a rotating backup or restore to a new empty directory."""
import argparse
from pathlib import Path
import sys
import os
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--restore', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--postgres', action='store_true', help='Restore into the empty database in DATABASE_URL')
    args = parser.parse_args()
    from backend.services.backups import create_backup, restore_backup
    if args.restore:
        if not args.destination:
            parser.error('--restore requires --destination (an empty directory)')
        from backend.core.config import get_database_uri
        print(restore_backup(args.restore, args.destination,
                             postgres_url=get_database_uri() if args.postgres else None))
    else:
        from backend import create_app
        with create_app().app_context():
            print(create_backup())
