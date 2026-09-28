"""页面路由：首页 / 学校详情 / 通知详情 / 设置 / 学校管理"""
from datetime import datetime, timedelta
from collections import OrderedDict
from urllib.parse import urlencode

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, g, make_response)
from sqlalchemy import extract, false, func
from sqlalchemy.orm import joinedload, defer

from backend.database.db import db
from backend.database.models import (School, Department, Announcement,
                                     Subscription, UserAnnouncementState)
from backend.auth import admin_required
from backend.services.article_provenance import article_provenance
from backend.services.summaries import current_summaries, summary_status

bp = Blueprint('pages', __name__)


@bp.route('/')
def index():
    """通知收件箱：订阅学校的统一通知流与组合筛选"""
    from backend.services import read_state
    from backend.services.inbox import (filtered_inbox, source_hierarchy, read_expression,
                                        inbox_mailbox, normalize_inbox_args, state_expression)
    from backend.services.directory_options import directory_entries_for, expand_directory_ids
    from backend.services.announcement_sources import (source_expression, source_counts,
                                                      sources_for, schools_for, memberships)
    from backend.services.inbox_refresh import refresh_status

    if g.get('user') is None:
        return redirect(url_for('pages.explore'))

    canonical_args = normalize_inbox_args(request.args)
    if canonical_args != request.args:
        return redirect(url_for('pages.index') + '?' + urlencode(list(canonical_args.items(multi=True))))

    subscriptions = (Subscription.query
                     .filter_by(user_id=g.user.id)
                     .join(School, Subscription.school_id == School.id)
                     .filter(School.enabled.is_(True))
                     .order_by(School.name)
                     .all())
    school_ids = [item.school_id for item in subscriptions]
    schools = [item.school for item in subscriptions]
    view = inbox_mailbox(request.args)
    reading_view = 'focus' if request.args.get('view') == 'focus' else 'split'
    retained_query = Announcement.query.filter(
        state_expression(g.user.id, 'archived'), source_expression())
    if view == 'saved':
        state_query = Announcement.query.filter(
            state_expression(g.user.id, 'starred'), source_expression())
        schools = schools_for(state_query)
    else:
        schools = sorted({s.id: s for s in schools + schools_for(retained_query)}.values(),
                         key=lambda school: school.name)
    school_ids = [s.id for s in schools]

    requested_school_id = request.args.get('school', type=int)
    current_school = next(
        (school for school in schools if school.id == requested_school_id),
        None,
    )
    current_school_id = current_school.id if current_school else None

    departments = []
    known_departments = []
    selected_dept_ids = []
    directory_entries = []
    if current_school:
        directory_entries = directory_entries_for(current_school.id)
        departments = (Department.query
                       .filter_by(school_id=current_school.id)
                       .filter(Department.name != 'DAILY NEWS')
                       .order_by(Department.id)
                       .all())
        known_departments = departments
        current_subscription = next((s for s in subscriptions if s.school_id == current_school.id), None)
        if view == 'inbox' and current_subscription and current_subscription.department_ids is not None:
            allowed = set(expand_directory_ids(current_school.id, current_subscription.department_ids, directory_entries))
            retained_sources = memberships(retained_query.with_entities(Announcement.id).statement)
            allowed.update(row[0] for row in db.session.query(retained_sources.c.department_id).distinct())
            departments = [d for d in departments if d.id in allowed]
        valid_dept_ids = {dept.id for dept in departments}
        selected_dept_ids = [dept_id for dept_id in expand_directory_ids(current_school.id,
                             request.args.getlist('dept', type=int), directory_entries)
                             if dept_id in valid_dept_ids]

    current_group = request.args.get('group', '').strip() if current_school else ''
    q = request.args.get('q', '').strip()[:200]
    period = request.args.get('period', 'all' if view != 'inbox' else 'week')
    if period not in ('week', 'archive', 'all'):
        period = 'week'
    current_year = request.args.get('year', type=int)
    current_month = request.args.get('month', type=int)
    if current_month not in range(1, 13):
        current_month = None
    if period == 'archive' and not current_year:
        period = 'week'
        current_month = None

    read_filter = request.args.get('read', 'all')
    if read_filter not in ('all', 'unread', 'read'):
        read_filter = 'all'

    page = max(1, request.args.get('page', 1, type=int))
    per_page = 40

    query = filtered_inbox(g.user.id, request.args, include_read=False)
    read_exists = read_expression(g.user.id)
    scope_count = query.count()
    scope_unread = query.filter(~read_exists).count()

    if read_filter == 'unread':
        query = query.filter(~read_exists)
    elif read_filter == 'read':
        query = query.filter(read_exists)

    pagination = (query
                  .options(joinedload(Announcement.school), joinedload(Announcement.department),
                           defer(Announcement.content_html), defer(Announcement.content_text))
                  .order_by(Announcement.published_at.desc().nullslast(),
                            Announcement.created_at.desc())
                  .paginate(page=page, per_page=per_page, error_out=False))
    announcements = pagination.items
    announcement_sources = sources_for([a.id for a in announcements])
    selected_ids = set(selected_dept_ids)
    allowed_subscriptions = {s.school_id: s.department_ids for s in subscriptions}
    def source_priority(source):
        selected = source.id in selected_ids if selected_ids else source.school_id == current_school_id
        allowed = (source.school_id in allowed_subscriptions and
                   (allowed_subscriptions[source.school_id] is None or source.id in allowed_subscriptions[source.school_id]))
        return (not selected, not allowed, source.school_id, source.id)
    for sources in announcement_sources.values():
        sources.sort(key=source_priority)
    display_sources = {key: value[0] for key, value in announcement_sources.items() if value}

    requested_ann_id = request.args.get('selected', type=int)
    selected_announcement = next(
        (ann for ann in announcements if ann.id == requested_ann_id),
        None,
    )
    detail_requested = bool(requested_ann_id and selected_announcement and
                            selected_announcement.id == requested_ann_id)
    if detail_requested:
        if read_state.mark_read(g.user.id, selected_announcement.id):
            scope_unread = max(0, scope_unread - 1)

    read_ids = read_state.read_ids_for(
        g.user.id,
        [ann.id for ann in announcements],
    )
    states = {s.announcement_id: s for s in UserAnnouncementState.query.filter(
        UserAnnouncementState.user_id == g.user.id,
        UserAnnouncementState.announcement_id.in_([a.id for a in announcements])).all()}
    # One compact excerpt per visible row, without loading full documents into the list.
    excerpts = dict(db.session.query(Announcement.id, func.substr(Announcement.content_text, 1, 150))
                    .filter(Announcement.id.in_([a.id for a in announcements])).all())

    grouped = OrderedDict()
    today = datetime.utcnow().date()
    for ann in announcements:
        timestamp = ann.published_at or ann.created_at
        if not timestamp:
            label = '时间未知'
        elif timestamp.date() == today:
            label = '今天'
        elif timestamp.date() == today - timedelta(days=1):
            label = '昨天'
        else:
            label = timestamp.strftime('%Y年%m月%d日')
        grouped.setdefault(label, []).append(ann)

    years_query = db.session.query(
        extract('year', Announcement.published_at).label('year'))
    if school_ids:
        years_query = years_query.filter(source_expression(school_ids=school_ids))
    else:
        years_query = years_query.filter(false())
    if current_school_id:
        years_query = years_query.filter(
            source_expression(school_ids=[current_school_id]))
    available_years = [int(row[0]) for row in
                       years_query.filter(Announcement.published_at.isnot(None))
                       .group_by('year').order_by(db.text('year DESC')).all()
                       if row[0]]

    dept_counts = {}
    if current_school_id:
        dept_counts = source_counts(current_school_id)

    def inbox_url(selected=None, page_number=None):
        params = []
        if reading_view == 'focus':
            params.append(('view', 'focus'))
            if view != 'inbox':
                params.append(('mailbox', view))
        elif view != 'inbox':
            params.append(('view', view))
        if current_group:
            params.append(('group', current_group))
        if q:
            params.append(('q', q))
        if current_school_id:
            params.append(('school', current_school_id))
        params.extend(('dept', dept_id) for dept_id in selected_dept_ids)
        # Keep the active time range explicit: favorites have a different
        # default, and fresh browser entries can inherit a user's preference.
        params.append(('period', period))
        if period == 'archive' and current_year:
            params.append(('year', current_year))
            if current_month:
                params.append(('month', current_month))
        if read_filter != 'all':
            params.append(('read', read_filter))
        if selected:
            params.append(('selected', selected))
            page_number = page
        if page_number and page_number > 1:
            params.append(('page', page_number))
        query_string = urlencode(params, doseq=True)
        return url_for('pages.index') + ('?' + query_string if query_string else '')

    notice_urls = {ann.id: inbox_url(selected=ann.id) for ann in announcements}
    previous_url = inbox_url(page_number=pagination.prev_num) if pagination.has_prev else None
    next_url = inbox_url(page_number=pagination.next_num) if pagination.has_next else None

    response = make_response(render_template(
        '_inbox_workspace.html' if request.headers.get('X-Inbox-Fragment') == '1' else 'index.html',
        schools=schools,
        departments=departments,
        source_tree=source_hierarchy(departments, known_departments, directory_entries),
        source_sync_states={s['id']: s for s in refresh_status(departments)['sources']},
        current_group=current_group,
        view=view,
        reading_view=reading_view,
        q=q,
        states=states,
        excerpts=excerpts,
        summary_texts=current_summaries(announcements),
        summary_state=summary_status(selected_announcement) if selected_announcement else None,
        subscribed_school_ids={s.school_id for s in subscriptions},
        dept_counts=dept_counts,
        current_school=current_school,
        current_school_id=current_school_id,
        selected_dept_ids=selected_dept_ids,
        period=period,
        current_year=current_year,
        current_month=current_month,
        read_filter=read_filter,
        available_years=available_years,
        grouped=grouped,
        announcements=announcements,
        announcement_sources=announcement_sources,
        display_sources=display_sources,
        selected_announcement=selected_announcement,
        selected_provenance=article_provenance(
            selected_announcement.content_html, selected_announcement.url) if selected_announcement else None,
        detail_requested=detail_requested,
        read_ids=read_ids,
        scope_count=scope_count,
        scope_unread=scope_unread,
        pagination=pagination,
        notice_urls=notice_urls,
        back_to_list_url=inbox_url(page_number=page),
        previous_url=previous_url,
        next_url=next_url,
    ))
    response.vary.add('X-Inbox-Fragment')
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/explore')
def explore():
    """学校目录（公开）：全部上架学校 + 当前用户订阅状态"""
    from backend.services import read_state
    from backend.database.models import Subscription

    from backend.services.catalog import catalog_entries, normalize_name
    schools = School.query.filter(School.enabled.is_(True)).order_by(School.name).all()
    ids = [s.id for s in schools]
    totals = read_state.total_count_by_school(ids)
    my_subs = ({s.school_id for s in
                Subscription.query.filter_by(user_id=g.user.id).all()}
               if g.get('user') else set())
    by_name = {normalize_name(s.name): s for s in School.query.all()}
    entries, matched = [], set()
    counts = dict(db.session.query(Department.school_id, func.count(Department.id)).group_by(Department.school_id))
    for entry in catalog_entries():
        school = by_name.get(normalize_name(entry['name']))
        if school and not school.enabled:
            continue
        if school:
            matched.add(school.id)
        entries.append(dict(entry, school=school))
    for school in schools:
        if school.id not in matched:
            entries.append(dict(name=school.name, url=school.url, province='', double_first_class=False, school=school))
    q = request.args.get('q', '').strip()[:100]
    province = request.args.get('province', '')
    level = request.args.get('level', 'all')
    provinces = sorted({e['province'] for e in entries if e['province']})
    total_catalog = len(entries)
    if q:
        term = normalize_name(q).lower()
        entries = [e for e in entries if term in e['name'].lower() or q.lower() in e['url'].lower()]
    if province:
        entries = [e for e in entries if e['province'] == province]
    if level == 'double':
        entries = [e for e in entries if e['double_first_class']]
    elif level == 'subscribed':
        entries = [e for e in entries if e['school'] and e['school'].id in my_subs]
    entries.sort(key=lambda e: (not (e['school'] and e['school'].id in my_subs), e['name']))
    page = max(1, request.args.get('page', 1, type=int))
    total = len(entries)
    pages = max(1, (total + 29) // 30)
    page = min(page, pages)
    entries = entries[(page - 1) * 30:page * 30]
    return render_template('explore.html', entries=entries, q=q, province=province, level=level,
                           provinces=provinces, total=total, total_catalog=total_catalog,
                           page=page, pages=pages, counts=counts, totals=totals, my_subs=my_subs)


@bp.route('/school/<int:school_id>')
def school_detail(school_id):
    """Public source preview; subscribers read through the unified inbox."""
    from backend.services.inbox import source_groups
    from backend.services.announcement_sources import source_expression, sources_for, preferred_source
    school = db.get_or_404(School, school_id)
    if not school.enabled:
        from flask import abort
        abort(404)
    departments = school.departments.order_by(Department.id).all()
    dept_id = request.args.get('dept', type=int)
    daily = any(d.id == dept_id and d.name.upper() == 'DAILY NEWS' for d in departments)
    if daily:
        dept_id = None
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    q = request.args.get('q', '').strip()[:200]
    if g.get('user') and Subscription.query.filter_by(
            user_id=g.user.id, school_id=school.id).first():
        return redirect(url_for('pages.index', school=school.id, dept=dept_id,
            period='week' if daily else ('archive' if year else 'all'),
            year=year, month=month, q=q, page=request.args.get('page', 1, type=int)))
    query = Announcement.query.filter(source_expression(school_ids=[school.id],
                                      department_ids=[dept_id] if dept_id else None))
    if year:
        query = query.filter(extract('year', Announcement.published_at) == year)
        if month in range(1, 13):
            query = query.filter(extract('month', Announcement.published_at) == month)
    if daily:
        query = query.filter(func.coalesce(Announcement.published_at, Announcement.created_at)
                             >= datetime.utcnow() - timedelta(days=7))
    if q:
        query = query.filter(Announcement.title.contains(q, autoescape=True))
    pagination = query.options(joinedload(Announcement.department),
        defer(Announcement.content_html), defer(Announcement.content_text)).order_by(
            Announcement.published_at.desc().nullslast(), Announcement.created_at.desc()
        ).paginate(page=max(1, request.args.get('page', 1, type=int)), per_page=30, error_out=False)
    sources = sources_for([a.id for a in pagination.items])
    display_sources = {a.id: preferred_source(a, sources.get(a.id, []), school.id, dept_id)
                       for a in pagination.items}
    return render_template('school.html', school=school, groups=source_groups(departments),
        pagination=pagination, current_dept=dept_id, q=q, year=year, month=month,
        announcement_sources=sources, display_sources=display_sources)


@bp.route('/announcement/<int:ann_id>')
def announcement_detail(ann_id):
    """通知详情页 — 显示全文 + AI摘要"""
    from backend.services.announcement_sources import source_expression, sources_for, preferred_source
    ann = Announcement.query.filter(Announcement.id == ann_id, source_expression()).first()
    if not ann:
        flash('通知不存在', 'error')
        return redirect(url_for('pages.index'))

    # 标记为已读（per-user；匿名只读不写）
    from backend.services import read_state
    if g.get('user'):
        read_state.mark_read(g.user.id, ann.id)

    # 返回地址：优先用列表页带来的 ?from=（保留部门/年份/分页等筛选状态），
    # 仅接受站内相对路径防开放重定向；缺省时回落到该通知所属部门列表
    sources = sources_for([ann.id]).get(ann.id, [])
    display_source = preferred_source(ann, sources, department_id=request.args.get('source', type=int))
    back_url = request.args.get('from', '')
    if not (back_url.startswith('/') and not back_url.startswith('//')):
        back_url = url_for('pages.school_detail', school_id=display_source.school_id,
                           dept=display_source.id)

    return render_template('announcement.html', announcement=ann, back_url=back_url,
                           summary_state=summary_status(ann),
                           sources=sources, display_source=display_source,
                           provenance=article_provenance(ann.content_html, ann.url))


@bp.route('/settings')
@bp.route('/settings/legacy')
@admin_required
def settings_page():
    """旧入口 → 统一后台"""
    return redirect('/admin#platform')


@bp.route('/schools/manage')
@admin_required
def schools_manage():
    """旧入口 → 统一后台"""
    return redirect('/admin#schools')
