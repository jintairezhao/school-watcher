"""Check current selectors without paid exploration or silent rule replacement.

Quiet columns are healthy. Broken rules are reported for the user's manual
school change check; old notices and subscriptions remain available.
"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from backend.core import DATA_DIR

logger = logging.getLogger(__name__)

AUDIT_PATH = Path(DATA_DIR) / 'selector_audit.json'
_lock = threading.Lock()

# 结构损坏阈值：列表项匹配少于此数视为「选择器失效」
BROKEN_MATCH_THRESHOLD = 3


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
    """标记部门待人工复核（不自动换选择器）。"""
    data = _load_audit()
    data['review'][str(department.id)] = {
        'dept_name': department.name,
        'school_id': department.school_id,
        'reason': reason,
        'marked_at': datetime.now(timezone.utc).isoformat(),
    }
    _save_audit(data)
    logger.warning(f"[选择器监督] {department.name} 标记待人工复核: {reason}")


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
    from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result
    result = classify_result(FetchRequest('https://example.edu.cn/', purpose='list'),
                             FetchResult('https://example.edu.cn/', status=200, html=html))
    return result.outcome in ('requires_render', 'needs_manual', 'denied') or not html.strip()


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
    """Return health or a request for manual checking; never enqueue paid work."""
    stats = _quick_stats(html, department)
    if stats is None:
        return {'action': 'skip'}

    if stats['matched'] >= 1 and stats['junk'] == 0:
        clear_review(department)
        return {'action': 'healthy', **stats}

    if is_challenge_shell(html):
        mark_needs_review(department, '抓到 WAF 挑战壳页，需浏览器环境或人工处理')
        return {'action': 'browser_needed', **stats}

    if stats['matched'] >= BROKEN_MATCH_THRESHOLD and stats['junk'] > 0:
        # 结构在但抓错：不自动换（防漂移到别的栏目），交给人
        mark_needs_review(department,
                          f"选择器匹配 {stats['matched']} 项但含垃圾标题 {stats['junk']} 条")
        return {'action': 'needs_review', **stats}

    # Ordinary collection only reports breakage. A manual school change check
    # owns deeper exploration and any paid fallback for this column.
    mark_needs_review(department, '栏目规则可能已失效，可在栏目订阅中点击“检查官网变化”')
    return {'action': 'needs_review', **stats}
