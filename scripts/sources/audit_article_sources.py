"""Audit explicit source signatures in saved articles, without changing records."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.services.article_provenance import article_provenance


def audit(database, school_id=None):
    states, labels = Counter(), Counter()
    observed, unresolved = [], []
    where = ' WHERE school_id=?' if school_id is not None else ''
    args = (school_id,) if school_id is not None else ()
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        # One read transaction keeps the report internally consistent while ingestion continues.
        connection.execute('BEGIN')
        for row in connection.execute('SELECT id,school_id,url,content_html FROM announcements' + where + ' ORDER BY id', args):
            result = article_provenance(row['content_html'], row['url'])
            states[result['state']] += 1
            record = {'announcement_id': row['id'], 'school_id': row['school_id'], 'url': row['url'], **result}
            if result['labels']:
                observed.append(record)
                labels.update(result['labels'])
            else:
                unresolved.append({'announcement_id': row['id'], 'state': result['state']})
    return {'audited_at': datetime.now(timezone.utc).isoformat(),
            'scope': 'Saved article HTML only; signatures are literal, not current unit identities or verified publishing responsibility.',
            'school_id': school_id, 'total_articles': sum(states.values()), 'states': dict(states),
            'signature_labels': dict(labels.most_common()), 'observed': observed, 'unresolved': unresolved}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'data' / 'school_watcher.db')
    parser.add_argument('--school-id', type=int)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.database, args.school_id)
    output = args.output or ROOT / 'data' / 'source-audits' / (
        'article-signatures-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('total_articles', 'states', 'signature_labels')} |
                     {'audit': str(output.resolve())}, ensure_ascii=True))
