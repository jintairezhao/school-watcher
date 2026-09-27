from backend.services.summaries import summary_expression
"""Shared inbox scope for reading and bulk actions; keep source names intact."""
from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import and_, or_, false, extract, func

from backend.database.db import db
from backend.database.models import Announcement, Department, School, Subscription, UserRead, UserAnnouncementState
from backend.services.announcement_sources import source_expression


def source_groups(departments):
    """Flat selectors use the same official grouping as the inbox tree."""
    groups = {}
    for group, units in source_hierarchy(departments).items():
        unique = {c['department'].id: c['department'] for u in units for c in u['columns']}
        groups[group] = list(unique.values())
    return groups


def source_hierarchy(departments, known_departments=None, directory_entries=()):
    """Project official directory paths without changing source IDs or subscriptions.

    Explicit directory memberships take precedence. Published website paths then
    supply missing ancestors; legacy unit-column metadata is the fallback. Each
    node exposes distinct descendant columns for filtering and its own columns
    for rendering, so an attached unit can remain a collapsible child.
    """
    from backend.services.source_placements import official_source_placements
    departments = list(departments)
    known = sorted(known_departments if known_departments is not None else departments,
                   key=lambda d: len(d.name), reverse=True)
    placements = official_source_placements(known)
    by_id = {d.id: d for d in known}
    parents = {}
    directory_ids = set()
    for entry in directory_entries:
        parent, child = by_id.get(entry.parent_id), by_id.get(entry.department_id)
        if parent and child and parent.school_id == child.school_id and parent.id != child.id:
            parents.setdefault(child.id, []).append((parent, entry.position))
            directory_ids.add(parent.id)
    groups = {}

    def put(group, nodes, column, *, official=False, directory=False):
        siblings = groups.setdefault(group, {})
        chain = [str(column['department'].school_id), group]
        for node in nodes:
            chain.append(str(node['key']))
            key = ('official-' + sha256('\0'.join(chain).encode()).hexdigest()[:20]
                   if official else ('directory-' + sha256('\0'.join(chain).encode()).hexdigest()[:20]
                   if directory and len(chain) > 3 else str(node['key'])))
            unit = siblings.setdefault(key, {'key': key, 'name': node['name'],
                'expandable': official or directory, 'directory': directory,
                'position': node.get('position', 0),
                'own_columns': [], 'children': {}})
            siblings = unit['children']
        if not any(c['department'].id == column['department'].id for c in unit['own_columns']):
            unit['own_columns'].append(column)
        return unit

    def directory_paths(owner, seen=frozenset()):
        if owner.id in seen:
            return []
        node = {'key': owner.id, 'name': owner.name}
        ancestors = parents.get(owner.id, [])
        if not ancestors:
            return [((owner.group_name or '').strip() or '其他栏目', [node])]
        return [(group, chain + [{**node, 'position': position}])
                for ancestor, position in ancestors
                for group, chain in directory_paths(ancestor, seen | {owner.id})]

    for dept in departments:
        if dept.name.upper() == 'DAILY NEWS':
            continue
        if dept.id in directory_ids:
            continue
        if dept.id in parents:
            for owner, position in parents[dept.id]:
                for group, chain in directory_paths(owner):
                    put(group, chain, {'department': dept, 'label': dept.name, 'position': position,
                        'unavailable': not bool(dept.list_url)}, directory=True)
            continue
        owner = next((item for item in known if item.school_id == dept.school_id and
                      any(dept.name.startswith(item.name + separator) for separator in ('-', '－', '—'))), dept)
        is_child = owner.id != dept.id
        label = dept.name[len(owner.name) + 1:].strip() if is_child else dept.name
        paths = placements.get(dept.id) or (placements.get(owner.id) if is_child else None)
        if paths:
            for path in paths:
                put(path['group'], path['nodes'], {'department': dept,
                    'label': path['label'] if dept.id in placements else label}, official=True)
        else:
            unit = put((owner.group_name or '').strip() or '其他栏目',
                       [{'key': owner.id, 'name': owner.name}], {'department': dept, 'label': label})
            unit['expandable'] |= is_child

    def finish(unit):
        from backend.services.student_sources import display_priority
        unit['children'] = [finish(child) for child in unit['children'].values()]
        unit['expandable'] |= bool(unit['children'])
        if unit['directory']:
            unit['children'].sort(key=lambda child: child['position'])
            unit['own_columns'].sort(key=lambda c: c['position'])
        elif unit['expandable']:
            for column in unit['own_columns']:
                if str(column['department'].id) == unit['key']:
                    column['label'] = '本部门通知'
        if not unit['directory']:
            unit['own_columns'].sort(key=lambda column: display_priority(column['label']))
            unit['children'].sort(key=lambda child: display_priority(child['name']))
        columns = {c['department'].id: c for c in unit['own_columns']}
        for child in unit['children']:
            for column in child['columns']:
                columns.setdefault(column['department'].id, column)
        unit['columns'] = list(columns.values())
        return unit

    from backend.services.student_sources import display_priority
    ordered = {group: sorted([finish(unit) for unit in units.values()],
                             key=lambda unit: display_priority(unit['name']))
               for group, units in groups.items()}
    def group_priority(item):
        group, units = item
        # A broad official organisation directory also contains the teaching
        # office and graduate school. Its generic heading must not bury them.
        return min([display_priority(group)] + [display_priority(unit['name'])
                   for unit in units if unit['expandable']])
    return dict(sorted(ordered.items(), key=group_priority))


def read_expression(user_id):
    return db.session.query(UserRead.user_id).filter(
        UserRead.user_id == user_id, UserRead.announcement_id == Announcement.id).exists()


def state_expression(user_id, field):
    return db.session.query(UserAnnouncementState.user_id).filter(
        UserAnnouncementState.user_id == user_id,
        UserAnnouncementState.announcement_id == Announcement.id,
        getattr(UserAnnouncementState, field).is_(True)).exists()


def subscription_scope(user_id):
    from backend.services.directory_options import expand_directory_ids
    subs = (Subscription.query.join(School).filter(
        Subscription.user_id == user_id, School.enabled.is_(True)).all())
    conditions = []
    for sub in subs:
        conditions.append(source_expression(school_ids=[sub.school_id],
                          department_ids=expand_directory_ids(sub.school_id, sub.department_ids)))
    return or_(*conditions) if conditions else false()


def inbox_mailbox(args):
    """Keep legacy mailbox URLs while allowing a focused reading presentation."""
    view = args.get('view', 'inbox')
    if view == 'focus':
        view = args.get('mailbox', 'inbox')
    return view if view in ('inbox', 'saved', 'archived') else 'inbox'


def filtered_inbox(user_id, args, include_read=True):
    """Parameters are shared with the GET page; invalid source IDs cannot widen scope."""
    view = inbox_mailbox(args)
    query = Announcement.query.filter(source_expression())
    # Saved records remain accessible after unsubscribing from a source.
    if view == 'saved':
        query = query.filter(state_expression(user_id, 'starred'))
    elif view == 'archived':
        query = query.filter(state_expression(user_id, 'archived'))
    else:
        query = query.filter(subscription_scope(user_id), ~state_expression(user_id, 'archived'))
    school_id = args.get('school', type=int)
    if school_id:
        from backend.services.directory_options import directory_entries_for, expand_directory_ids
        entries = directory_entries_for(school_id)
        school_departments = Department.query.filter_by(school_id=school_id).all()
        valid_ids = {d.id for d in school_departments}
        dept_ids = expand_directory_ids(school_id, [i for i in args.getlist('dept', type=int) if i in valid_ids], entries)
        group = args.get('group', '').strip()
        if group:
            group_ids = {column['department'].id for unit in source_hierarchy(school_departments, directory_entries=entries).get(group, [])
                         for column in unit['columns']}
            dept_ids = [i for i in dept_ids if i in group_ids] if args.getlist('dept') else list(group_ids)
        query = query.filter(source_expression(school_ids=[school_id],
                            department_ids=dept_ids if group or any(args.getlist('dept')) else None))
    period = args.get('period', 'all' if view != 'inbox' else 'week')
    if period not in ('week', 'all', 'archive'):
        period = 'week'
    year = args.get('year', type=int)
    month = args.get('month', type=int)
    timestamp = func.coalesce(Announcement.published_at, Announcement.created_at)
    if period == 'week' or (period == 'archive' and not year):
        query = query.filter(timestamp >= datetime.utcnow() - timedelta(days=7))
    elif period == 'archive':
        query = query.filter(extract('year', timestamp) == year)
        if month in range(1, 13):
            query = query.filter(extract('month', timestamp) == month)
    for keyword in args.get('q', '').strip()[:200].split()[:10]:
        query = query.filter(or_(
            Announcement.title.contains(keyword, autoescape=True),
            summary_expression().contains(keyword, autoescape=True),
            Announcement.content_text.contains(keyword, autoescape=True)))
    if include_read:
        if args.get('read') == 'unread':
            query = query.filter(~read_expression(user_id))
        elif args.get('read') == 'read':
            query = query.filter(read_expression(user_id))
    return query
