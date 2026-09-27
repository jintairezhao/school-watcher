"""Offline identity backfill, preserving every membership and personal state."""
from datetime import datetime
import hashlib

import sqlalchemy as sa

from backend.services.source_inventory import canonical_url


def merge_announcement_duplicates(connection):
    metadata = sa.MetaData()
    metadata.reflect(connection)
    articles = metadata.tables['announcements']
    mappings = metadata.tables['announcement_merges']
    rows = list(connection.execute(sa.select(articles).order_by(articles.c.id.desc())).mappings())
    appearances = metadata.tables.get('announcement_sources')
    if appearances is not None:
        departments = metadata.tables['departments']
        urls = dict(connection.execute(sa.select(departments.c.id, departments.c.list_url)).all())
        known = set(connection.execute(sa.select(appearances.c.announcement_id, appearances.c.department_id)).all())
        for row in rows:
            if (row['id'], row['department_id']) not in known:
                stamp = row.get('created_at') or datetime.utcnow()
                connection.execute(appearances.insert().values(announcement_id=row['id'], department_id=row['department_id'],
                    list_url=urls.get(row['department_id']) or '', article_url=row.get('url') or '',
                    first_seen_at=stamp, last_seen_at=stamp))
    survivors, aliases = {}, {}
    for source in rows:
        row = dict(source)
        try:
            canonical = canonical_url(row['url']) if row.get('url') else None
        except (ValueError, UnicodeError):
            canonical = None
        key = hashlib.sha256(canonical.encode()).hexdigest() if canonical else None
        identity = (row['school_id'], canonical)
        if not key or identity not in survivors:
            connection.execute(articles.update().where(articles.c.id == row['id']).values(
                canonical_url=canonical or None, url_key=key))
            if key:
                survivors[identity] = row
            continue
        keeper = survivors[identity]
        old, new = row['id'], keeper['id']
        aliases[old] = new
        # Highest ID survives so even SQLite's legacy non-AUTOINCREMENT table
        # retains its high-water mark. Never reuse deleted duplicate IDs here.
        values = {}
        for field in ('published_at', 'summary'):
            if not keeper.get(field) and row.get(field):
                values[field] = row[field]
        if row.get('created_at') and (not keeper.get('created_at') or row['created_at'] < keeper['created_at']):
            values['created_at'] = row['created_at']
        if row.get('content_html') or row.get('content_text'):
            has_body = keeper.get('content_html') or keeper.get('content_text')
            newer = (row.get('content_cached_at') or datetime.min) > (keeper.get('content_cached_at') or datetime.min)
            if not has_body or newer:
                for field in ('content_html', 'content_text', 'content_hash', 'content_bytes',
                              'content_cached_at', 'content_accessed_at', 'content_error'):
                    if field in articles.c:
                        values[field] = row.get(field)
        values['is_updated'] = bool(keeper.get('is_updated') or row.get('is_updated'))
        if values:
            connection.execute(articles.update().where(articles.c.id == new).values(**values))
            keeper.update(values)
        for table_name in ('announcement_sources', 'user_reads', 'user_announcement_states'):
            table = metadata.tables.get(table_name)
            if table is None:
                continue
            dimension = 'department_id' if table_name == 'announcement_sources' else 'user_id'
            old_rows = list(connection.execute(sa.select(table).where(table.c.announcement_id == old)).mappings())
            for old_row in old_rows:
                where = sa.and_(table.c.announcement_id == new, table.c[dimension] == old_row[dimension])
                existing = connection.execute(sa.select(table).where(where)).mappings().first()
                if existing is None:
                    selector = sa.and_(table.c.announcement_id == old, table.c[dimension] == old_row[dimension])
                    connection.execute(table.update().where(selector).values(announcement_id=new))
                else:
                    merged = {}
                    if table_name == 'announcement_sources':
                        merged['first_seen_at'] = min(existing['first_seen_at'], old_row['first_seen_at'])
                        merged['last_seen_at'] = max(existing['last_seen_at'], old_row['last_seen_at'])
                        for field in ('list_url', 'article_url'):
                            merged[field] = existing[field] or old_row[field]
                    elif table_name == 'user_announcement_states':
                        merged = {field: bool(existing[field] or old_row[field]) for field in ('starred', 'archived')}
                    else:
                        stamps = [v for v in (existing['read_at'], old_row['read_at']) if v]
                        merged['read_at'] = min(stamps) if stamps else None
                    connection.execute(table.update().where(where).values(**merged))
                    connection.execute(table.delete().where(sa.and_(table.c.announcement_id == old,
                                                                     table.c[dimension] == old_row[dimension])))
        connection.execute(mappings.insert().values(old_id=old, survivor_id=new, merged_at=datetime.utcnow()))
        connection.execute(articles.delete().where(articles.c.id == old))
    if aliases and 'background_tasks' in metadata.tables:
        tasks = metadata.tables['background_tasks']
        for task in connection.execute(sa.select(tasks)).mappings().all():
            payload = dict(task['payload'] or {})
            old = payload.get('announcement_id')
            if old in aliases:
                payload['announcement_id'] = aliases[old]
                identity = f"content:{aliases[old]}" if task['kind'] == 'content' else task['identity']
                existing = connection.execute(sa.select(tasks.c.id).where(tasks.c.identity == identity)).scalar()
                if existing and existing != task['id']:
                    connection.execute(tasks.delete().where(tasks.c.id == task['id']))
                    continue
                connection.execute(tasks.update().where(tasks.c.id == task['id']).values(
                    identity=identity, payload=payload, state='pending', token=None, lease_until=None))
            if isinstance(payload.get('ids'), list):
                payload['ids'] = list(dict.fromkeys(aliases.get(i, i) for i in payload['ids']))
                connection.execute(tasks.update().where(tasks.c.id == task['id']).values(payload=payload))
    return aliases
