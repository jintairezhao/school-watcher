"""DeepSeek API 摘要模块

使用 OpenAI 兼容接口调用 DeepSeek API，
对通知内容进行智能摘要（不超过100字）。
"""

import logging
import time
from openai import OpenAI

from backend.database.models import AppConfig

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = """你是一个高校通知摘要助手。请用不超过100字总结以下学校通知的核心内容。

要求：
1. 保留关键日期、时间节点
2. 点明涉及的事项或活动
3. 如有行动要求（如报名、提交材料等），必须提及
4. 语言简洁，信息密度高
5. 只输出摘要文本，不要加任何前缀或引号

通知标题：{title}

通知正文：
{content}
"""


def get_client():
    """获取 DeepSeek 客户端"""
    from backend.core.secrets import decrypt_field
    api_key = decrypt_field(AppConfig.get('deepseek_api_key') or '')
    if not api_key:
        raise ValueError("DeepSeek API Key 未配置，请在设置页面添加")

    return OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )


def summarize_announcement(title: str, content_text: str, max_retries: int = 3) -> str:
    """对单条通知生成AI摘要"""
    if not content_text or len(content_text.strip()) < 20:
        return "（内容过短，无需摘要）"

    # 截取正文前3000字，避免token浪费
    truncated = content_text[:3000]

    client = get_client()

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "你是一个专业的摘要生成助手，输出简洁准确。"},
                    {"role": "user", "content": SUMMARY_PROMPT.format(
                        title=title, content=truncated
                    )},
                ],
                max_tokens=200,
                temperature=0.3,
            )
            summary = response.choices[0].message.content.strip()
            # 清理可能的引号
            summary = summary.strip('"\'').strip()
            return summary

        except Exception as e:
            logger.error(f"DeepSeek API 调用失败 (尝试 {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return "（AI摘要生成失败，请稍后重试）"

    return "（AI摘要生成失败）"


def batch_summarize(announcement_ids: list = None):
    """批量生成摘要（用于新抓取的通知或未生成摘要的通知）"""
    from backend.database.db import db
    from backend.database.models import Announcement

    if announcement_ids:
        announcements = Announcement.query.filter(
            Announcement.id.in_(announcement_ids)
        ).all()
    else:
        # 获取所有未生成摘要的通知
        announcements = Announcement.query.filter(
            (Announcement.summary == None) | (Announcement.summary == '')
        ).all()

    if not announcements:
        logger.info("没有需要生成摘要的通知")
        return 0

    logger.info(f"开始批量生成摘要: {len(announcements)} 条通知")
    count = 0

    for ann in announcements:
        try:
            summary = summarize_announcement(ann.title, ann.content_text or '')
            if summary and not summary.startswith('（AI摘要生成失败'):
                ann.summary = summary
                db.session.commit()
                count += 1
                logger.info(f"摘要生成成功 [{ann.id}]: {ann.title[:30]}...")
            # 避免请求过快
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"摘要生成异常 [{ann.id}]: {e}")
            db.session.rollback()

    logger.info(f"批量摘要完成: {count}/{len(announcements)} 条成功")
    return count
