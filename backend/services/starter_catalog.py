"""Public school configuration only: never subscriptions, notices or credentials."""
from functools import lru_cache
import json
from pathlib import Path

from flask import current_app
from backend.core.config import ROOT_DIR
from backend.database.db import db
from backend.database.models import AppConfig, Department, DepartmentDirectoryEntry
from backend.services.source_inventory import canonical_url

FIELDS = ('name', 'list_url', 'group_name', 'list_selector', 'title_selector',
          'link_selector', 'date_selector', 'content_selector')


@lru_cache(maxsize=4)
def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def catalog():
    path = current_app.config.get('STARTER_CATALOG_PATH', ROOT_DIR / 'config' / 'desktop-schools.json')
    return _read(str(path))


def install():
    """Idempotent additive installation, preserving any existing local edits."""
    from backend.services.school_registry import ensure_school
    source = catalog()
    marker = 'desktop_catalog:' + source['version']
    if AppConfig.get(marker) == 'installed':
        return
    for item in source['schools']:
        school, _ = ensure_school(item['name'], item['url'], origin='desktop-catalog')
        existing = list(school.departments)
        mapped = {}
        for entry in item['departments']:
            matches = [d for d in existing if d.name == entry['name'] and
                       canonical_url(d.list_url or '') == canonical_url(entry['list_url'] or '')]
            variants = [e for e in item['departments'] if (e['name'], e['list_url']) == (entry['name'], entry['list_url'])]
            match = (matches[0] if len(matches) == len(variants) == 1 else
                     next((d for d in matches if (d.list_selector or '') == entry['list_selector']), None))
            if match is None:
                match = Department(school_id=school.id, **{key: entry.get(key, '') for key in FIELDS})
                db.session.add(match)
                db.session.flush()
                existing.append(match)
            mapped[entry['key']] = match.id
        for entry in item['directory_entries']:
            parent, child = mapped[entry['parent']], mapped[entry['child']]
            if parent != child and db.session.get(DepartmentDirectoryEntry, (parent, child)) is None:
                db.session.add(DepartmentDirectoryEntry(parent_id=parent, department_id=child, position=entry['position']))
        db.session.commit()
    AppConfig.set(marker, 'installed')


def placements(school_url, source):
    """Display the bundled classification until a local discovery publishes newer paths."""
    for school in catalog()['schools']:
        if canonical_url(school['url']) != canonical_url(school_url):
            continue
        for entry in school['departments']:
            if (entry['name'], entry['group_name'], canonical_url(entry['list_url'] or '')) == (
                    source.name, source.group_name or '', canonical_url(source.list_url or '')):
                return entry.get('placements', [])
    return []
