"""订阅 API — 订阅驱动抓取

规则：有人订阅才抓取；0→1 复活立即后台首抓；归零暂停（调度过滤生效）。
subscriber_count 为缓存列，在订阅事务内用 SQL 算术维护。
"""
import logging
import threading

from flask import Blueprint, g, jsonify, request, current_app

from backend.database.db import db
from backend.database.models import School, Subscription
from backend.auth import login_required

logger = logging.getLogger(__name__)

bp = Blueprint('subscriptions', __name__)


def start_background_scrape(school_id):
    """后台立即抓取（首抓/复活共用，无 SSE）"""
    from backend.scraper.engine import scrape_school
    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                scrape_school(db.session.get(School, school_id))
            except Exception as e:
                logger.error(f"后台抓取失败 [school {school_id}]: {e}")

    threading.Thread(target=_run, daemon=True).start()


@bp.route('/api/subscriptions', methods=['GET'])
@login_required
def api_my_subscriptions():
    subs = (Subscription.query.filter_by(user_id=g.user.id)
            .order_by(Subscription.created_at).all())
    return jsonify([{
        'school_id': s.school_id,
        'school_name': s.school.name,
        'created_at': s.created_at.isoformat() if s.created_at else None,
    } for s in subs])


@bp.route('/api/subscriptions', methods=['POST'])
@login_required
def api_subscribe():
    data = request.get_json(silent=True) or {}
    school = db.session.get(School, data.get('school_id') or 0)
    if not school or not school.enabled:
        return jsonify({'error': '学校不存在或已下架'}), 404

    if Subscription.query.filter_by(user_id=g.user.id,
                                    school_id=school.id).first():
        return jsonify({'success': True, 'subscribed': True,
                        'subscriber_count': school.subscriber_count or 0})

    revived = (school.subscriber_count or 0) == 0
    db.session.add(Subscription(user_id=g.user.id, school_id=school.id))
    db.session.execute(
        db.update(School).where(School.id == school.id)
        .values(subscriber_count=School.subscriber_count + 1))
    db.session.commit()

    if revived:
        logger.info(f"[订阅] {school.name} 0→1 复活，立即首抓")
        start_background_scrape(school.id)

    fresh = db.session.get(School, school.id)
    return jsonify({'success': True, 'subscribed': True,
                    'subscriber_count': fresh.subscriber_count or 0,
                    'scrape_started': revived})


@bp.route('/api/subscriptions/<int:school_id>', methods=['DELETE'])
@login_required
def api_unsubscribe(school_id):
    sub = Subscription.query.filter_by(user_id=g.user.id,
                                       school_id=school_id).first()
    if not sub:
        return jsonify({'success': True, 'subscribed': False})

    db.session.delete(sub)
    db.session.execute(
        db.update(School).where(School.id == school_id)
        .values(subscriber_count=db.func.max(School.subscriber_count - 1, 0)))
    db.session.commit()

    school = db.session.get(School, school_id)
    if (school.subscriber_count or 0) == 0:
        logger.info(f"[订阅] {school.name} 订阅归零，暂停抓取")
    return jsonify({'success': True, 'subscribed': False,
                    'subscriber_count': school.subscriber_count or 0})
