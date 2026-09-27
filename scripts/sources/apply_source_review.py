"""Submit an evidence-backed metadata review without changing active source rules.

Dry run by default. A manifest contains school_id, school_url, updates (id,
expected, config), and additions. No deletion or subscription rewrite is allowed.
Each config carries evidence_file and evidence_sha256 relative to the manifest.
--apply persists review proposals; the shared worker independently verifies and
activates them. A local HTML match alone cannot bypass source governance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup
from backend import create_app
from backend.database.db import db
from backend.database.models import Department, School
from backend.services.source_catalog import FIELDS
from backend.services.source_inventory import canonical_url
from backend.scraper.http_client import validate_public_url


def validate_config(config, folder):
    if not isinstance(config.get('name'), str) or not 1 <= len(config['name']) <= 200:
        raise ValueError('来源名称无效')
    for field in FIELDS:
        if not isinstance(config.get(field, ''), str):
            raise ValueError('来源字段无效：' + field)
    validate_public_url(config['list_url'], resolve=False)
    evidence = (folder / config['evidence_file']).resolve()
    if not evidence.is_relative_to(folder.resolve()) or not evidence.is_file():
        raise ValueError('缺少本次核对的网页证据')
    payload = evidence.read_bytes()
    if hashlib.sha256(payload).hexdigest() != config['evidence_sha256']:
        raise ValueError('核对网页已改变，请重新审核')
    html = payload.decode('utf-8')
    matches = BeautifulSoup(html, 'lxml').select(config.get('list_selector') or ':not(*)')
    if not matches or not any(node.get_text(strip=True) for node in matches):
        raise ValueError('核对网页未匹配到栏目内容：' + config['name'])
    return {field: config.get(field, '') for field in ('name', *FIELDS)}


def apply_review(manifest, folder, *, apply=False):
    school = db.session.get(School, manifest['school_id'])
    if not school or canonical_url(school.url) != canonical_url(manifest['school_url']):
        raise ValueError('学校身份与核对清单不一致')
    existing = school.departments.all()
    by_id = {d.id: d for d in existing}
    edits, additions, unchanged = [], [], 0
    seen_ids = set()
    for change in manifest.get('updates', []):
        ident = change['id']
        if ident in seen_ids or ident not in by_id:
            raise ValueError('核对清单的来源编号重复或不属于本校')
        seen_ids.add(ident)
        row = by_id[ident]
        config = validate_config(change['config'], folder)
        if all(getattr(row, key) == value for key, value in config.items()):
            unchanged += 1
            continue
        expected = change.get('expected', {})
        if set(expected) != {'name', *FIELDS} or any(getattr(row, key) != value for key, value in expected.items()):
            raise ValueError(f'来源 {ident} 在核对后已被修改，请重新核对')
        edits.append((row, config))
    identities = {(canonical_url(d.list_url or ''), d.list_selector or '') for d in existing}
    for change in manifest.get('additions', []):
        config = validate_config(change, folder)
        identity = (canonical_url(config['list_url']), config['list_selector'])
        if identity in identities:
            unchanged += 1
            continue
        identities.add(identity)
        additions.append(config)
    summary = {'school': school.name, 'updates': len(edits), 'additions': len(additions), 'unchanged': unchanged}
    if apply:
        from backend.services.source_governance import propose_source
        from backend.services.tasks import enqueue
        proposals = []
        try:
            for row, config in edits:
                proposals.append(propose_source(school.id, config, department_id=row.id,
                                                origin='review_manifest', commit=False))
            for config in additions:
                proposals.append(propose_source(school.id, config, origin='review_manifest', commit=False))
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        for proposal in proposals:
            enqueue('source_review', proposal.id, {'proposal_id': proposal.id})
        summary['proposal_ids'] = [proposal.id for proposal in proposals]
        summary['activated'] = 0
    return dict(summary, applied=apply)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.environ['WATCHER_SEED_ON_START'] = '0'
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    with create_app().app_context():
        print(json.dumps(apply_review(manifest, args.manifest.parent, apply=args.apply), ensure_ascii=False))


if __name__ == '__main__':
    main()
