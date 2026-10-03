"""Read-only channel navigation over the reader's existing message scope."""
from collections import OrderedDict
import unicodedata

from flask import g, request, url_for
from sqlalchemy import case, func
from werkzeug.datastructures import MultiDict

from backend.database.db import db
from backend.database.models import Announcement, Department, School, Subscription
from backend.services.announcement_sources import memberships, source_expression
from backend.services.inbox import filtered_inbox, inbox_mailbox, read_expression, state_expression


def channel_url(source, view=None):
    if not g.get('user'):
        return url_for('pages.school_detail', school_id=source.school_id, dept=source.id)
    return url_for('pages.index', school=source.school_id, dept=source.id,
                   period='all', **({'view': 'saved'} if (view or inbox_mailbox(request.args)) == 'saved' else {}))


def channel_breadcrumb(source):
    if source is None:
        return ''
    from backend.services.source_channels import channels_for
    return channels_for([source])[source.id]['breadcrumb']


def _publication_cache():
    current = request._get_current_object()
    if getattr(g, '_channel_publication_request', None) is not current:
        g._channel_publication_request = current
        g._channel_publications = {}
    return g._channel_publications


def prime_publications(announcement_ids):
    """Fetch all displayed memberships together; never start background work."""
    from backend.services.announcement_sources import publication_links_for
    from backend.services.source_channels import channels_for
    cache = _publication_cache()
    missing = set(announcement_ids) - cache.keys()
    if not missing:
        return
    records = publication_links_for(missing)
    sources = {row['source'].id: row['source'] for rows in records.values() for row in rows}
    descriptors = channels_for(sources.values())
    for ident in missing:
        cache[ident] = [dict(descriptors[row['source'].id], **row,
                            filter_url=channel_url(row['source'])) for row in records.get(ident, [])]


def publication_links(announcement_id):
    prime_publications([announcement_id])
    return _publication_cache().get(announcement_id, [])


def channel_overview(user_id, *, school_id=None, q='', view='inbox'):
    """Each channel counts a shared notice once; the total counts it once overall."""
    from backend.services.inbox_refresh import subscribed_sources
    from backend.services.source_channels import channels_for
    view = 'saved' if view == 'saved' else 'inbox'
    readable = filtered_inbox(user_id, MultiDict({'period': 'all', 'view': view}))
    retained = readable.filter(state_expression(user_id, 'starred' if view == 'saved' else 'archived'))
    retained_refs = memberships(retained.with_entities(Announcement.id).statement)
    available = {d.id: d for d in Department.query.join(
        retained_refs, retained_refs.c.department_id == Department.id).join(School).filter(School.enabled.is_(True))}
    if view == 'inbox':
        subscriptions = Subscription.query.join(School).filter(
            Subscription.user_id == user_id, School.enabled.is_(True)).all()
        for sub in subscriptions:
            available.update((d.id, d) for d in subscribed_sources(sub.school, sub.department_ids))

    # This is a directory of actual publishing channels, not empty organisation nodes.
    available = {ident: d for ident, d in available.items()
                 if d.kind not in ('unit', 'group') and d.name.upper() != 'DAILY NEWS'}
    refs = memberships(readable.with_entities(Announcement.id).statement)
    read = read_expression(user_id)
    stats = {ident: {'count': count, 'unread_count': unread, 'latest_at': latest}
             for ident, count, unread, latest in db.session.query(
                 refs.c.department_id, func.count(Announcement.id),
                 func.sum(case((read, 0), else_=1)),
                 func.max(Announcement.published_at))
             .join(Announcement, Announcement.id == refs.c.announcement_id)
             .filter(refs.c.department_id.in_(available)).group_by(refs.c.department_id)} if available else {}
    available = {ident: d for ident, d in available.items() if d.list_selector or ident in stats}
    schools = sorted({d.school_id: d.school for d in available.values()}.values(), key=lambda school: school.name)
    candidates = [d for d in available.values() if school_id is None or d.school_id == school_id]
    descriptors = channels_for(candidates)
    terms = unicodedata.normalize('NFKC', q).casefold().split()
    groups, shown = OrderedDict(), []
    for source in sorted(candidates, key=lambda d: (d.school.name,
                         descriptors[d.id]['publisher_label'], descriptors[d.id]['column_label'], d.id)):
        info = descriptors[source.id]
        haystack = unicodedata.normalize('NFKC', info['breadcrumb'] + ' ' + info['official_url']).casefold()
        if not all(term in haystack for term in terms):
            continue
        school = groups.setdefault(source.school_id, {'school': source.school, 'publishers': OrderedDict()})
        publisher = school['publishers'].setdefault(info['publisher_key'],
            {'key': info['publisher_key'], 'label': info['publisher_label'], 'channels': []})
        publisher['channels'].append(dict(info, filter_url=channel_url(source, view),
            **stats.get(source.id, {'count': 0, 'unread_count': 0, 'latest_at': None})))
        shown.append(source.id)
    for school in groups.values():
        school['publishers'] = list(school['publishers'].values())
    return dict(channel_schools=list(groups.values()), schools=schools, current_school_id=school_id,
                q=q, view=view, total_channels=len(shown),
                total_notices=readable.filter(source_expression(department_ids=shown)).count() if shown else 0)
