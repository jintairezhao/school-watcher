"""页面路由：首页 / 学校详情 / 通知详情 / 设置 / 学校管理"""
from datetime import datetime, timedelta
from collections import OrderedDict

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, g)
from sqlalchemy import extract

from backend.database.db import db
from backend.database.models import School, Announcement, ScrapeLog, AppConfig
from backend.services.dept_tree import build_dept_tree
from backend.auth import admin_required

bp = Blueprint('pages', __name__)


@bp.route('/')
def index():
    """首页 = 我的订阅流；匿名重定向到学校目录"""
    from backend.services import read_state
    from backend.database.models import Subscription

    if g.get('user') is None:
        return redirect(url_for('pages.explore'))

    school_ids = [s.school_id for s in
                  Subscription.query.filter_by(user_id=g.user.id).all()]
    schools = (School.query.filter(School.id.in_(school_ids)).all()
               if school_ids else [])
    totals = read_state.total_count_by_school(school_ids)
    reads = read_state.unread_count_by_school(g.user.id, school_ids)

    school_data = []
    for school in schools:
        latest = (Announcement.query
                  .filter_by(school_id=school.id)
                  .order_by(Announcement.created_at.desc())
                  .limit(3).all())
        school_data.append({
            'school': school,
            'latest': latest,
            'read_ids': read_state.read_ids_for(g.user.id, [a.id for a in latest]),
            'unread_count': max(0, totals.get(school.id, 0) - reads.get(school.id, 0)),
            'total_count': totals.get(school.id, 0),
        })

    total_unread = sum(d['unread_count'] for d in school_data)
    return render_template('index.html', school_data=school_data,
                           total_unread=total_unread)


@bp.route('/explore')
def explore():
    """学校目录（公开）：全部上架学校 + 当前用户订阅状态"""
    from backend.services import read_state
    from backend.database.models import Subscription

    schools = School.query.filter(School.enabled.is_(True)).order_by(School.name).all()
    ids = [s.id for s in schools]
    totals = read_state.total_count_by_school(ids)
    my_subs = ({s.school_id for s in
                Subscription.query.filter_by(user_id=g.user.id).all()}
               if g.get('user') else set())
    return render_template('explore.html', schools=schools,
                           totals=totals, my_subs=my_subs)


def _flatten_tree(tree):
    """展平分组树，返回所有叶子部门项列表"""
    result = []
    for item in tree:
        if item.get('type') == 'group':
            result.extend(item['depts'])
        else:
            result.append(item)
    return result


@bp.route('/school/<int:school_id>')
def school_detail(school_id):
    """学校详情页 — 按部门 → 年月分组展示通知"""
    school = db.session.get(School, school_id)
    if not school:
        flash('学校不存在', 'error')
        return redirect(url_for('pages.index'))

    dept_id = request.args.get('dept', type=int)
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = 50

    # 获取部门列表（含通知计数）并构建父子树
    departments = school.departments.all()
    dept_tree = build_dept_tree(departments)
    flat_tree = _flatten_tree(dept_tree)

    # 找到 DAILY NEWS 部门 ID
    daily_news_id = None
    for item in flat_tree:
        if item.get('is_daily_news'):
            daily_news_id = item['dept'].id
            break

    is_daily_news = bool(daily_news_id and dept_id == daily_news_id)

    # ---- DAILY NEWS 模式：聚合近 7 天全校通知 ----
    if is_daily_news:
        # 使用 naive datetime，与数据库中的 published_at 一致
        now = datetime.utcnow()
        cutoff = now - timedelta(days=7)
        h24 = now - timedelta(hours=24)
        h48 = now - timedelta(hours=48)
        h72 = now - timedelta(hours=72)

        def _daily_label(dt):
            if dt >= h24:
                return '📅 今天（24h内）'
            elif dt >= h48:
                return '📅 昨天'
            elif dt >= h72:
                return '📅 前天'
            else:
                return '📅 ' + dt.strftime('%Y年%m月%d日')

        query = Announcement.query.filter(
            Announcement.school_id == school_id,
            Announcement.published_at >= cutoff,
        )

        pagination = (
            query.order_by(Announcement.published_at.desc().nullslast(),
                           Announcement.created_at.desc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        announcements = pagination.items

        # 按 相对日期 → 部门 → 通知 三级分组
        grouped = OrderedDict()
        for ann in announcements:
            dept_name = ann.department.name if ann.department else '未分类'
            label = _daily_label(ann.published_at) if ann.published_at else '时间未知'

            if label not in grouped:
                grouped[label] = OrderedDict()
            if dept_name not in grouped[label]:
                grouped[label][dept_name] = []
            grouped[label][dept_name].append(ann)

        available_years = []

    else:
        # ---- 常规模式：按部门/年月筛选通知 ----
        query = Announcement.query.filter_by(school_id=school_id)
        if dept_id:
            # 如果筛选的是父部门，同时包含所有子部门的通知
            all_dept_ids = [dept_id]
            for item in flat_tree:
                if item['dept'].id == dept_id and item['has_children']:
                    all_dept_ids.extend(item['child_ids'])
                    break
            query = query.filter(Announcement.department_id.in_(all_dept_ids))
        if year:
            query = query.filter(extract('year', Announcement.published_at) == year)
            if month:
                query = query.filter(extract('month', Announcement.published_at) == month)

        pagination = (
            query.order_by(Announcement.published_at.desc().nullslast(),
                           Announcement.created_at.desc())
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        announcements = pagination.items

        # 构建 年月 → 部门 → 通知列表 的三级分组结构
        grouped = OrderedDict()
        for ann in announcements:
            dept_name = ann.department.name if ann.department else '未分类'
            if ann.published_at:
                ym_label = ann.published_at.strftime('%Y年%m月')
            else:
                ym_label = '时间未知'

            if ym_label not in grouped:
                grouped[ym_label] = OrderedDict()
            if dept_name not in grouped[ym_label]:
                grouped[ym_label][dept_name] = []
            grouped[ym_label][dept_name].append(ann)

        # 获取可选的年份列表（用于筛选，基于现有通知数据）
        years_query = (
            db.session.query(
                extract('year', Announcement.published_at).label('y')
            )
            .filter(Announcement.school_id == school_id)
            .filter(Announcement.published_at.isnot(None))
            .group_by('y')
            .order_by(db.text('y DESC'))
            .all()
        )
        available_years = [r[0] for r in years_query if r[0]]

    # 当前学校的未读通知数 + 本页已读集合（per-user；匿名无未读概念）
    from backend.services import read_state
    if g.get('user'):
        totals = read_state.total_count_by_school([school_id])
        reads = read_state.unread_count_by_school(g.user.id, [school_id])
        view_unread = max(0, totals.get(school_id, 0) - reads.get(school_id, 0))
        read_ids = read_state.read_ids_for(g.user.id, [a.id for a in announcements])
    else:
        view_unread = 0
        read_ids = set()

    # 当前用户是否已订阅本校（头部订阅按钮用）
    from backend.database.models import Subscription
    is_subscribed = bool(g.get('user') and Subscription.query.filter_by(
        user_id=g.user.id, school_id=school_id).first())

    return render_template(
        'school.html',
        school=school,
        dept_tree=dept_tree,
        departments=departments,
        grouped=grouped,
        pagination=pagination,
        current_dept=dept_id,
        current_year=year,
        current_month=month,
        available_years=available_years,
        is_daily_news=is_daily_news,
        daily_news_id=daily_news_id,
        view_unread=view_unread,
        read_ids=read_ids,
        is_subscribed=is_subscribed,
    )


@bp.route('/announcement/<int:ann_id>')
def announcement_detail(ann_id):
    """通知详情页 — 显示全文 + AI摘要"""
    ann = db.session.get(Announcement, ann_id)
    if not ann:
        flash('通知不存在', 'error')
        return redirect(url_for('pages.index'))

    # 标记为已读（per-user；匿名只读不写）
    from backend.services import read_state
    if g.get('user'):
        read_state.mark_read(g.user.id, ann.id)

    # 返回地址：优先用列表页带来的 ?from=（保留部门/年份/分页等筛选状态），
    # 仅接受站内相对路径防开放重定向；缺省时回落到该通知所属部门列表
    back_url = request.args.get('from', '')
    if not (back_url.startswith('/') and not back_url.startswith('//')):
        back_url = url_for('pages.school_detail', school_id=ann.school_id,
                           dept=ann.department_id)

    return render_template('announcement.html', announcement=ann, back_url=back_url)


@bp.route('/settings')
@admin_required
def settings_page():
    """旧入口 → 统一后台"""
    return redirect('/admin#platform')


@bp.route('/settings/legacy')
@admin_required
def settings_page_legacy():
    """设置页面"""
    schools = School.query.order_by(School.name).all()
    from backend.core.secrets import decrypt_field
    api_key = decrypt_field(AppConfig.get('deepseek_api_key', ''))
    # 仅传递脱敏后的 key（前4 + **** + 后4），完整 key 不暴露到前端
    if len(api_key) > 10:
        api_key_masked = api_key[:6] + '****' + api_key[-4:]
    else:
        api_key_masked = api_key
    interval = AppConfig.get('scrape_interval', '30')
    last_logs = ScrapeLog.query.order_by(ScrapeLog.started_at.desc()).limit(20).all()
    security_question = AppConfig.get('security_question', '')
    return render_template(
        'settings.html',
        schools=schools,
        api_key_masked=api_key_masked,
        api_key_has_value=bool(api_key),
        interval=interval,
        last_logs=last_logs,
        has_password=bool(AppConfig.get('app_password', '')),
        security_question=security_question,
        has_security_question=bool(security_question),
    )


@bp.route('/schools/manage')
@admin_required
def schools_manage():
    """旧入口 → 统一后台"""
    return redirect('/admin#schools')
