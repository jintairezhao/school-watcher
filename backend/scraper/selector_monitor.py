"""Check source health and queue bounded, validated repair for changed pages."""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.core import DATA_DIR
from backend.scraper.fetch_errors import SourceAccessError

logger = logging.getLogger(__name__)

AUDIT_PATH = Path(DATA_DIR) / 'selector_audit.json'
_lock = threading.Lock()

# 结构损坏阈值：列表项匹配少于此数视为「选择器失效」
BROKEN_MATCH_THRESHOLD = 3
REPAIR_INTERVAL = 6 * 60 * 60


class ParserRepairPending(SourceAccessError):
    """An observed rule failure handed off to the existing onboarding pipeline."""
    def __init__(self, job):
        self.job = job
        super().__init__('官网页面已变化，系统正在自动恢复读取；已有消息仍保留')


# ------------------------------------------------------------------
# 审计日志
# ------------------------------------------------------------------

def _load_audit() -> dict:
    if not AUDIT_PATH.exists():
        return {'changes': [], 'review': {}}
    try:
        return json.loads(AUDIT_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {'changes': [], 'review': {}}


def _save_audit(data: dict):
    with _lock:
        AUDIT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding='utf-8')


def record_selector_change(department, old: dict, new: dict, source: str, reason: str):
    """记录一次选择器变更（auto=监督器自动 / human=人工修正）。"""
    data = _load_audit()
    data['changes'].append({
        'ts': datetime.now(timezone.utc).isoformat(),
        'dept_id': department.id,
        'dept_name': department.name,
        'school_id': department.school_id,
        'source': source,
        'reason': reason,
        'old': old,
        'new': new,
    })
    data['changes'] = data['changes'][-200:]  # 只留最近 200 条
    _save_audit(data)
    logger.info(f"[选择器审计] {department.name}: {source} 变更 ({reason})")


def mark_needs_review(department, reason: str):
    """保留异常诊断，不把解析和修复责任交给读者。"""
    data = _load_audit()
    data['review'][str(department.id)] = {
        'dept_name': department.name,
        'school_id': department.school_id,
        'reason': reason,
        'marked_at': datetime.now(timezone.utc).isoformat(),
    }
    _save_audit(data)
    logger.warning(f"[选择器监督] {department.name}: {reason}")


def clear_review(department):
    data = _load_audit()
    if str(department.id) in data['review']:
        del data['review'][str(department.id)]
        _save_audit(data)


# ------------------------------------------------------------------
# 健康评估与修复
# ------------------------------------------------------------------

def is_challenge_shell(html: str) -> bool:
    """Only content evidence can identify a shell; short valid lists are valid."""
    result = _classify_page(html)
    return result.outcome in ('requires_render', 'needs_manual', 'denied') or not html.strip()


def _classify_page(html, url='https://example.edu.cn/'):
    from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
    raw = getattr(html, 'result', None)
    return classify_result(FetchRequest(url, purpose='list'), FetchResult(url, status=200,
        html=str(html), transport=getattr(raw, 'transport', 'http')))


def queue_parser_repair(department, html=None):
    """Revalidate this known page, at most once per six hours after a repair.

    No selectors are installed here. The onboarding worker must execute the
    proposed extraction and verify an actual article before changing the source.
    """
    import hashlib
    from backend.database.models import BackgroundTask, Subscription
    from backend.services import tasks
    from backend.services.inbox_refresh import subscribed_sources
    from backend.services.source_governance import _snapshot
    from backend.services.source_inventory import canonical_url
    from backend.scraper.http_client import same_school_url, validate_public_url
    school = department.school
    if not school or not school.is_effectively_active() or not department.list_url:
        return None
    scopes = Subscription.query.filter_by(school_id=school.id).all()
    if not any(department.id in {d.id for d in subscribed_sources(school, sub.department_ids)} for sub in scopes):
        return None
    if html is not None:
        outcome = _classify_page(html, getattr(html, 'final_url', department.list_url)).outcome
        if outcome in ('empty', 'requires_render', 'needs_manual', 'denied') or not str(html).strip():
            return None
    previous = canonical_url(department.list_url)
    url = canonical_url(getattr(html, 'final_url', previous))
    try:
        validate_public_url(url, resolve=False)
    except ValueError:
        return None
    external = not same_school_url(previous, school.url)
    if not same_school_url(url, school.url) and not (external and same_school_url(url, previous)):
        return None
    key = str(school.id) + ':' + hashlib.sha256(url.encode()).hexdigest()[:24]
    old = BackgroundTask.query.filter_by(identity='onboard:' + key).first()
    if old and old.state not in ('done', 'failed'):
        return old
    if old and old.payload.get('automatic_repair'):
        finished = old.finished_at or old.checked_at or old.updated_at
        if finished and finished > datetime.utcnow() - timedelta(seconds=REPAIR_INTERVAL):
            return old
    current = tasks.current_execution() or {}
    assistance = current.get('payload', {}).get('ai_assist') is not False
    discovery = BackgroundTask.query.filter_by(identity=f'discover:{school.id}').first()
    if discovery and discovery.payload.get('ai_assist') is False:
        assistance = False
    payload = {'school_id': school.id, 'url': url, 'label': department.name,
        'group_name': department.group_name or '', 'revalidate': True,
        'automatic_repair': True, 'repair_department_id': department.id,
        'previous_url': previous if previous != url else None,
        'ai_assist': assistance, 'official_external': not same_school_url(url, school.url)}
    if html is not None:
        payload['snapshot'] = _snapshot(html, url, role='list')
    return tasks.enqueue('onboard', key, payload, replace_finished=True,
        min_interval=REPAIR_INTERVAL if old and old.payload.get('automatic_repair') else 0)


def _quick_stats(html: str, department):
    """用部门当前选择器快速统计：匹配数 + 垃圾标题数（只查前 10 项）。"""
    from bs4 import BeautifulSoup
    from backend.scraper.detectors.title_quality import (
        is_junk_title, extract_best_title, clean_title)
    from backend.scraper.discovery.publication_lists import select_node
    from backend.scraper.change_detector import parse_date
    from urllib.parse import urljoin

    if not department.list_selector:
        return None
    soup = BeautifulSoup(html, 'lxml')
    try:
        items = soup.select(department.list_selector)
    except Exception:
        return None
    junk = 0
    for item in items[:10]:
        anchor = select_node(item, department.link_selector or 'a[href]')
        date_el = select_node(item, department.date_selector) if department.date_selector else None
        dated = parse_date(date_el.get_text(strip=True)) if date_el is not None else None
        article_url = urljoin(getattr(department, '_fetch_final_url', department.list_url), anchor.get('href', '')) if anchor is not None and dated else ''
        el = item.select_one(department.title_selector) if department.title_selector else item
        title = ''
        if el:
            attr = (el.get('title') or el.get('data-title') or '').strip()
            title = clean_title(attr or el.get_text(strip=True))
        if is_junk_title(title, article_url=article_url):
            title = clean_title(extract_best_title(item, article_url=article_url))
        if is_junk_title(title, article_url=article_url) or not title:
            junk += 1
    return {'matched': len(items), 'junk': junk}


def evaluate_and_repair(department, html: str) -> dict:
    """Healthy/empty pages stay quiet; actual rule breakage repairs one page."""
    outcome = _classify_page(html, department.list_url).outcome
    if outcome in ('requires_render', 'needs_manual', 'denied') or not html.strip():
        mark_needs_review(department, '官网暂时限制自动访问，等待访问恢复；已有消息仍保留')
        return {'action': 'browser_needed'}
    if outcome == 'empty':
        clear_review(department)
        return {'action': 'healthy', 'matched': 0, 'junk': 0}
    try:
        stats = _quick_stats(html, department)
    except Exception:
        stats = None  # Invalid saved selectors use the same validated repair.
    if stats and stats['matched'] >= 1 and stats['junk'] == 0:
        clear_review(department)
        return {'action': 'healthy', **stats}
    job = queue_parser_repair(department, html)
    department._parser_repair_job = job
    mark_needs_review(department, '官网读取规则已变化，系统将自动重新识别并验证；已有通知继续保留')
    return {'action': 'repairing' if job and job.state in ('pending', 'running', 'waiting') else 'deferred',
            'repair_task_id': job.id if job else None, **(stats or {})}
