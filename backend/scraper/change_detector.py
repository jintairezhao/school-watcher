"""智能变更检测模块

使用三重策略确保不遗漏任何变更：
1. 内容哈希 - 标题+正文前500字的MD5
2. URL指纹 - 相同URL视为同一条
3. 发布时间比较 - 新发布时间覆盖旧记录
"""

import hashlib
from datetime import datetime, timezone
from backend.database.models import Announcement
from backend.database.db import db
from backend.scraper.cms_registry import parse_date


def compute_hash(title: str, content_text: str, attachments: list | None = None) -> str:
    """计算内容哈希（标题 + 正文文字 + 附件链接）。

    附件链接纳入哈希：正文文字相同的两条通知若附件不同（如 PDF 版本更新、
    海报图片更换），应视为两条不同通知，而不是被误判为重复。
    """
    sample = (title or '') + (content_text or '')[:500]
    if attachments:
        sample += '|' + '|'.join(sorted(set(attachments)))
    return hashlib.md5(sample.encode('utf-8')).hexdigest()


def is_duplicate(url: str, title: str, content_text: str) -> bool:
    """检查是否为重复公告"""
    if url:
        existing = Announcement.query.filter_by(url=url).first()
        if existing:
            return True

    new_hash = compute_hash(title, content_text)
    existing = Announcement.query.filter_by(content_hash=new_hash).first()
    if existing:
        return True

    return False


def detect_update(url: str, title: str, content_text: str | None = None):
    """检测是否有内容更新，返回 (existing_announcement, is_updated)"""
    if not url:
        return None, False

    existing = Announcement.query.filter_by(url=url).first()
    if not existing:
        return None, False

    # A listing-only visit has not observed the article body. It cannot prove a
    # content change and must never erase the previously fetched document.
    if content_text is None:
        return existing, False

    new_hash = compute_hash(title, content_text)
    if existing.content_hash != new_hash:
        # 内容已更新
        existing.content_hash = new_hash
        existing.title = title
        existing.content_text = content_text
        existing.is_updated = True
        existing.created_at = datetime.now(timezone.utc)
        db.session.commit()
        return existing, True

    return existing, False


# parse_date 统一由 cms_registry 提供（数据来自 cms_profiles.yaml）。
# 此处 re-export，供 engine.py 等旧有 `from ...change_detector import parse_date`
# 引用保持兼容。
