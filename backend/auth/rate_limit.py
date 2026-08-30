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
    now = datetime.utcnow()
    bucket = db.session.get(RateBucket, key)

    if bucket is None:
        db.session.add(RateBucket(key=key, count=1, window_start=now))
        db.session.commit()
        return True, max_count - 1

    if now - bucket.window_start > timedelta(seconds=window_seconds):
        bucket.window_start = now
        bucket.count = 1
        db.session.commit()
        return True, max_count - 1

    bucket.count += 1
    db.session.commit()
    allowed = bucket.count <= max_count
    return allowed, max(0, max_count - bucket.count)
