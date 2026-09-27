"""Compatibility entrypoints for explicit, instance-wide summary jobs.

No truncation, hidden retries or failure messages stored as successful text.
"""
from uuid import uuid4


def summarize_announcement(title: str, content_text: str, max_retries: int = 1) -> str:
    """One bounded call; the durable worker handles longer notices in chunks."""
    from backend.ai.configuration import get_model_binding
    from backend.ai.runtime import run_skill
    from backend.services.summaries import _paragraphs, _validate_output, CHUNK_CHARACTERS, SKILL_ID
    if not content_text or len(content_text.strip()) < 20:
        raise ValueError('正文较短，无需生成摘要')
    if len(content_text) > CHUNK_CHARACTERS:
        raise ValueError('长文摘要请使用可恢复的共享摘要任务')
    paragraphs = _paragraphs(content_text)
    result = run_skill(SKILL_ID, mode='summary', evidence={'title': title, 'paragraphs': paragraphs,
        'attachments_unread': True}, purpose='summary', execution_id='summary:explicit:' + uuid4().hex,
        binding=get_model_binding('summary'))
    if result.get('status') != 'succeeded':
        raise ValueError(result.get('error') or '摘要生成未成功，请查看调用记录')
    return _validate_output(result['output'], paragraphs)['summary'].strip()


def batch_summarize(announcement_ids=None):
    """Old worker hook now only schedules an explicitly supplied set of IDs."""
    from backend.services.summaries import enqueue_batch
    return enqueue_batch(announcement_ids)['count']
