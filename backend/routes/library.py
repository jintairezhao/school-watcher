"""Personal favorites and inbox-scoped bulk read actions."""
from flask import Blueprint, g, jsonify, request, url_for
from backend.database.dialect import insert

from backend.auth import login_required
from backend.database.db import db
from backend.database.models import Announcement, School, UserAnnouncementState, UserRead
from backend.services.inbox import filtered_inbox, read_expression, normalize_inbox_args

bp = Blueprint('library', __name__)


@bp.get('/api/inbox/search-suggestions')
@login_required
def search_suggestions():
    """Read-only previews use precisely the inbox's permission and filter scope."""
    from backend.services.announcement_sources import sources_for
    from backend.services.summaries import summary_expression

    keyword = request.args.get('q', '').strip()[:200]
    if not keyword:
        response = jsonify(query='', items=[], has_more=False)
    else:
        rows = (filtered_inbox(g.user.id, request.args)
                .with_entities(Announcement.id, Announcement.title, Announcement.published_at,
                               Announcement.content_text, summary_expression().label('summary'))
                .order_by(Announcement.published_at.desc().nullslast(), Announcement.created_at.desc())
                .limit(9).all())
        sources = sources_for([row.id for row in rows[:8]])
        args = normalize_inbox_args(request.args)
        params = {key: args.getlist(key) for key in
                  ('school', 'dept', 'group', 'period', 'year', 'month', 'view', 'mailbox', 'read')
                   if key in args}
        params['q'] = keyword
        terms = keyword.lower().split()[:10]

        def excerpt(*texts):
            for value in texts:
                value = (value or '').strip()
                positions = [value.lower().find(term) for term in terms]
                positions = [position for position in positions if position >= 0]
                if positions:
                    start = max(0, min(positions) - 28)
                    end = min(len(value), start + 160)
                    return ('…' if start else '') + value[start:end] + ('…' if end < len(value) else '')
            return ''

        items = []
        for row in rows[:8]:
            # Sources are metadata; the query above determines visibility, including
            # saved notifications from schools the reader has unsubscribed from.
            choices = sources.get(row.id, [])
            school_id = request.args.get('school', type=int)
            source = next((item for item in choices if item.school_id == school_id), choices[0] if choices else None)
            items.append({'id': row.id, 'title': row.title,
                          'source': f'{source.school.name} · {source.name}' if source else '',
                          'date': row.published_at.strftime('%Y-%m-%d') if row.published_at else '',
                          'snippet': excerpt(row.summary, row.content_text),
                          'url': url_for('pages.index', **params, selected=row.id)})
        response = jsonify(query=keyword, items=items, has_more=len(rows) > 8)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@bp.route('/api/announcements/<int:ann_id>/state', methods=['PUT'])
@login_required
def update_state(ann_id):
    ann = db.session.get(Announcement, ann_id)
    from backend.services.announcement_sources import source_expression
    if not ann or not Announcement.query.filter(Announcement.id == ann_id, source_expression()).first():
        return jsonify(error='通知不存在或来源已下架'), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data or set(data) - {'starred'}:
        return jsonify(error='请选择收藏操作'), 400
    if any(type(value) is not bool for value in data.values()):
        return jsonify(error='状态必须为 true 或 false'), 400
    db.session.execute(insert(UserAnnouncementState).values(
        user_id=g.user.id, announcement_id=ann_id, **data).on_conflict_do_update(
            index_elements=['user_id', 'announcement_id'], set_=data))
    db.session.commit()
    if data.get('starred'):
        from backend.services.content_cache import request_content
        request_content(ann)
    return jsonify(success=True, **data)


@bp.route('/api/inbox/read', methods=['POST'])
@login_required
def mark_filtered_read():
    from sqlalchemy import literal
    from datetime import datetime
    query = filtered_inbox(g.user.id, request.args).filter(~read_expression(g.user.id))
    rows = query.with_entities(literal(g.user.id), Announcement.id, literal(datetime.utcnow()))
    result = db.session.execute(insert(UserRead).from_select(
        ['user_id', 'announcement_id', 'read_at'], rows.statement).on_conflict_do_nothing())
    db.session.commit()
    return jsonify(success=True, count=result.rowcount)
