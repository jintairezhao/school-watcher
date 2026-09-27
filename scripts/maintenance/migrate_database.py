"""Copy all SQLite business data to an empty PostgreSQL deployment."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
from backend.services.database_transfer import sqlite_to_postgres


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Source SQLite database; kept unchanged')
    parser.add_argument('--catalog', help='Published SQLite catalogue next to its catalog-generations directory')
    parser.add_argument('--destination-data', required=True, help='Empty directory for published catalogue files')
    parser.add_argument('--stopped', action='store_true', help='Confirm web and every worker have been stopped')
    args = parser.parse_args()
    target = os.environ.get('WATCHER_MIGRATION_DATABASE_URL')
    if not target:
        parser.error('Set WATCHER_MIGRATION_DATABASE_URL to an EMPTY PostgreSQL database (credentials never enter command arguments)')
    try:
        report = sqlite_to_postgres(args.source, target, args.destination_data, catalog=args.catalog, stopped=args.stopped)
    except Exception as exc:
        # SQL errors may include row data and passwords. Do not echo their SQL parameters.
        import sqlalchemy as sa
        print('Migration stopped: ' + (type(exc).__name__ if isinstance(exc, sa.exc.SQLAlchemyError) else str(exc)), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('Keep the original SECRET_KEY and application encryption key when configuring the destination. Start only the destination after verification.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
