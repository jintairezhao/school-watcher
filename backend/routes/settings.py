"""平台设置 API（admin）与已读 API（登录用户）

密码/密保已迁至 per-user 账户体系（routes/auth.py 找回、routes/account.py 自服务，
account.py 于阶段 5 引入）；本文件只保留平台级设置与已读接口。
"""
import logging

from flask import Blueprint, request, jsonify, g

from backend.database.db import db
from backend.database.models import AppConfig, Announcement, Department
from backend.auth import admin_required, login_required

logger = logging.getLogger(__name__)

bp = Blueprint('settings', __name__)


@bp.route('/api/settings', methods=['POST'])
@admin_required
def api_save_settings():
    """保存平台设置（API Key / 抓取间隔）"""
    data = request.get_json()
    if not isinstance(data, dict) or not data:
        return jsonify({'error': '无效数据'}), 400

    if 'api_key' in data:
        return jsonify(error='请在系统管理的 AI 服务中配置、测试并选择用途'), 409

    from backend.services.collection_settings import validate_month, since_month
    month = None
    if 'since_month' in data:
        try:
            month = validate_month(data['since_month'])
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    interval = None
    if 'interval' in data:
        try:
            interval = int(data['interval'])
        except (TypeError, ValueError):
            return jsonify({'error': '间隔必须为整数（分钟）'}), 400
        if not 5 <= interval <= 720:
            return jsonify({'error': '间隔需在 5-720 分钟之间'}), 400
    if month is not None and month != since_month():
        # Backfills must not stop at the first already-known page.
        Department.query.update({Department.last_scraped_at: None})
        AppConfig.set('scrape_since_month', month)
    if interval is not None:
        AppConfig.set('scrape_interval', str(interval))
        # The independent worker rereads this persisted setting on every tick.

    return jsonify({'success': True, 'message': '设置已保存'})


@bp.route('/api/announcements/<int:ann_id>/read', methods=['POST'])
@login_required
def api_mark_read(ann_id):
    """标记为已读（仅本人）"""
    from backend.services import read_state
    from backend.services.announcement_sources import source_expression
    if not Announcement.query.filter(Announcement.id == ann_id, source_expression()).first():
        return jsonify(error='通知不存在'), 404
    read_state.mark_read(g.user.id, ann_id)
    return jsonify({'success': True})


@bp.route('/api/announcements/<int:ann_id>/read', methods=['DELETE'])
@login_required
def api_mark_unread(ann_id):
    """撤销已读（仅本人）"""
    from backend.services import read_state
    read_state.mark_unread(g.user.id, ann_id)
    return jsonify({'success': True})


@bp.route('/api/announcements/read-all', methods=['POST'])
@login_required
def api_mark_all_read():
    """一键已读（仅本人）：指定学校或我订阅的全部学校"""
    from backend.services import read_state
    from backend.database.models import Subscription
    try:
        data = request.get_json(silent=True) or {}
        school_id = data.get('school_id')
        if school_id:
            school_ids = [int(school_id)]
        else:
            school_ids = [s.school_id for s in
                          Subscription.query.filter_by(user_id=g.user.id).all()]
        count = read_state.mark_all_read(g.user.id, school_ids)
        return jsonify({
            'success': True,
            'count': count,
            'message': f'已标记 {count} 条通知为已读'
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"一键已读失败: {e}")
        return jsonify({'success': False, 'error': '操作失败，请稍后重试'}), 500
