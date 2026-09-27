"""Publish observed roster units as stable options, preserving shared memberships."""
from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry
from backend.scraper.http_client import validate_public_url
from backend.services.source_inventory import canonical_url, site_key


def directory_entries_for(school_id):
    return (DepartmentDirectoryEntry.query.join(Department, Department.id == DepartmentDirectoryEntry.parent_id)
            .filter(Department.school_id == school_id)
            .order_by(DepartmentDirectoryEntry.parent_id, DepartmentDirectoryEntry.position).all())


def expand_directory_ids(school_id, ids, entries=None):
    if ids is None:
        return None
    expanded = set(ids)
    entries = directory_entries_for(school_id) if entries is None else entries
    # Bound traversal by stored memberships, including defensive cycle handling.
    while True:
        children = {e.department_id for e in entries if e.parent_id in expanded}
        if children <= expanded:
            return sorted(expanded)
        expanded.update(children)


def sync_directory_options(school, catalog):
    """Worker-only metadata sync. No HTTP, no deletion or renaming of existing sources."""
    existing = school.departments.order_by(Department.id).all()
    directories = catalog.directory_entries(site_key(school.url), [d.list_url for d in existing])
    memberships = {(e.parent_id, e.department_id): e for e in directory_entries_for(school.id)}
    created = linked = 0
    for parent in list(existing):
        for position, node in enumerate(directories.get(canonical_url(parent.list_url or ''), [])):
            name, url = node.get('name', '').strip(), node.get('url', '').strip()
            if not name or len(name) > 200 or name == parent.name:
                continue
            if url:
                try:
                    validate_public_url(url, resolve=False)
                except ValueError:
                    continue
            unit = next((d for d in existing if d.name == name and
                         canonical_url(d.list_url or '') == canonical_url(url)), None)
            if unit is None:
                unit = Department(school_id=school.id, name=name, list_url=url,
                                  group_name=parent.group_name)
                db.session.add(unit); db.session.flush()
                existing.append(unit)
                created += 1
            key = (parent.id, unit.id)
            membership = memberships.get(key)
            if membership is None:
                membership = DepartmentDirectoryEntry(parent_id=parent.id, department_id=unit.id, position=position)
                db.session.add(membership)
                memberships[key] = membership
                linked += 1
            else:
                membership.position = position
    db.session.commit()
    return {'created_units': created, 'directory_memberships': linked}
