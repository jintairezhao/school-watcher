"""每用户已读状态服务（user_reads 表）

替代 announcements.is_read 全局位。全部函数以 user_id 为维度，
批量查询避免 N+1。
"""
from sqlalchemy import func

from backend.database.db import db
from backend.database.models import Announcement, UserRead
from backend.services.announcement_sources import school_memberships, source_expression


def read_ids_for(user_id, announcement_ids):
    """某用户对给定通知 id 集合的已读 id 集合"""
    if not user_id or not announcement_ids:
        return set()
    rows = (db.session.query(UserRead.announcement_id)
            .filter(UserRead.user_id == user_id,
                    UserRead.announcement_id.in_(announcement_ids))
            .all())
    return {r[0] for r in rows}


def unread_count_by_school(user_id, school_ids):
    """一次查询：用户对各学校已读通知数（未读 = 校总数 - 已读，由调用方算）"""
    if not user_id or not school_ids:
        return {}
    refs = school_memberships(school_ids)
    rows = (db.session.query(refs.c.school_id, func.count(UserRead.id))
            .join(UserRead, db.and_(
                UserRead.announcement_id == refs.c.announcement_id,
                UserRead.user_id == user_id))
            .group_by(refs.c.school_id)
            .all())
    return {sid: cnt for sid, cnt in rows if cnt}


def total_count_by_school(school_ids):
    """一次查询：各学校通知总数"""
    if not school_ids:
        return {}
    refs = school_memberships(school_ids)
    rows = (db.session.query(refs.c.school_id, func.count(refs.c.announcement_id))
            .group_by(refs.c.school_id)
            .all())
    return dict(rows)


def mark_read(user_id, announcement_id):
    """幂等标记单条已读"""
    if not user_id:
        return False
    if not UserRead.query.filter_by(user_id=user_id,
                                    announcement_id=announcement_id).first():
        db.session.add(UserRead(user_id=user_id, announcement_id=announcement_id))
        db.session.commit()
        return True
    return False


def mark_unread(user_id, announcement_id):
    """幂等撤销单条已读状态，仅影响当前用户"""
    if not user_id:
        return
    row = UserRead.query.filter_by(
        user_id=user_id,
        announcement_id=announcement_id,
    ).first()
    if row:
        db.session.delete(row)
        db.session.commit()


def mark_all_read(user_id, school_ids):
    """批量已读：INSERT…SELECT 只影响本人，OR IGNORE 兜幂等"""
    if not user_id or not school_ids:
        return 0
    from backend.services.inbox import subscription_scope
    unread = (db.session.query(Announcement.id)
              .filter(source_expression(school_ids=school_ids), subscription_scope(user_id),
                      ~Announcement.id.in_(
                          db.session.query(UserRead.announcement_id)
                          .filter(UserRead.user_id == user_id)))
              .all())
    ids = [r[0] for r in unread]
    for i in ids:
        db.session.add(UserRead(user_id=user_id, announcement_id=i))
    db.session.commit()
    return len(ids)
