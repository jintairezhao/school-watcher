"""通用限流（落库 RateBucket，跨 worker 共享）

key 维度约定：'login:<username>' / 'login:ip:<ip>' / 'register:ip:<ip>' /
'recovery:<username>' / 'submit:<user_id>' / 'submit:ip:<ip>'。
窗口过期自动清零，无需后台清理任务。
"""

from datetime import datetime, timedelta

from backend.database.db import db
from backend.database.models import RateBucket


def check_rate_limit(key: str, max_count: int, window_seconds: int):
    """计数并判阈值。返回 (allowed, remaining)。

    时间统一用 naive UTC（SQLite DateTime 不存时区，读回为 naive）。
    """
    from sqlalchemy import case
    from backend.database.dialect import insert
    now = datetime.utcnow()
    expired = RateBucket.window_start <= now - timedelta(seconds=window_seconds)
    statement = insert(RateBucket).values(key=key, count=1, window_start=now).on_conflict_do_update(
        index_elements=['key'], set_={
            'count': case((expired, 1), else_=RateBucket.count + 1),
            'window_start': case((expired, now), else_=RateBucket.window_start),
        }).returning(RateBucket.count)
    count = db.session.execute(statement).scalar_one()
    db.session.commit()
    return count <= max_count, max(0, max_count - count)
