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
        from backend.services.source_channels import channels_for
        channels = channels_for({source.id: source for entries in sources.values() for source in entries}.values())
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
            selected_ids = request.args.getlist('dept', type=int)
            source = min(choices, key=lambda item: (item.id not in selected_ids,
                item.school_id != school_id, item.id)) if choices else None
            items.append({'id': row.id, 'title': row.title,
                          'source': channels[source.id]['breadcrumb'] if source else '',
                          'date': row.published_at.strftime('%Y-%m-%d') if row.published_at else '',
                          'snippet': excerpt(row.summary, row.content_text),
                          'url': url_for('pages.index', **params, selected=row.id)})
        response = jsonify(query=keyword, items=items, has_more=len(rows) > 8)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@bp.route('/api/announcements/<int:ann_id>/state', methods=['PUT'])
@login_required
def update_state(ann_id):
    from backend.services.saved_materials import readable_expression, queue_materials, material_status
    ann = Announcement.query.filter(Announcement.id == ann_id, readable_expression(g.user.id)).with_for_update().first()
    if not ann:
        return jsonify(error='通知不存在或来源已下架'), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data or set(data) - {'starred'}:
        return jsonify(error='请选择收藏操作'), 400
    if any(type(value) is not bool for value in data.values()):
        return jsonify(error='状态必须为 true 或 false'), 400
    from datetime import datetime, timedelta
    from sqlalchemy import case
    from backend.services.saved_materials import TRASH_DAYS
    now = datetime.utcnow()
    db.session.execute(insert(UserAnnouncementState).values(
        user_id=g.user.id, announcement_id=ann_id, starred=data['starred'], trashed_at=None).on_conflict_do_update(
            index_elements=['user_id', 'announcement_id'], set_={'starred': data['starred'],
                'trashed_at': None if data['starred'] else case(
                    (UserAnnouncementState.starred.is_(True), now), else_=UserAnnouncementState.trashed_at)}))
    db.session.commit()
    if data.get('starred'):
        queue_materials(ann)
    state = db.session.get(UserAnnouncementState, (g.user.id, ann_id))
    expires = state.trashed_at + timedelta(days=TRASH_DAYS) if state.trashed_at else None
    return jsonify(success=True, **data, trash_until=expires.isoformat() + 'Z' if expires else None,
        archive=material_status(ann) if data['starred'] else None)


@bp.route('/api/announcements/<int:ann_id>/saved-materials', methods=['GET', 'POST'])
@login_required
def saved_materials_status(ann_id):
    from datetime import datetime, timedelta
    from backend.services.saved_materials import TRASH_DAYS, material_status, queue_materials
    state = db.session.get(UserAnnouncementState, (g.user.id, ann_id))
    expires = state.trashed_at + timedelta(days=TRASH_DAYS) if state and state.trashed_at else None
    active_trash = bool(expires and expires > datetime.utcnow())
    ann = db.session.get(Announcement, ann_id)
    if not ann or not state or (not state.starred and not active_trash):
        return jsonify(state='none', resources=[])
    if request.method == 'POST':
        from backend.auth.rate_limit import check_rate_limit
        if not check_rate_limit(f'saved-materials:user:{g.user.id}', 30, 3600)[0]:
            return jsonify(error='保存请求较频繁，请稍后重试'), 429
        queue_materials(ann, retry=True)
    result = material_status(ann)
    return jsonify(**result, starred=state.starred,
        trash_until=expires.isoformat() + 'Z' if active_trash else None)


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
