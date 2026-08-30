"""选择器健康监督器 — 质量触发的反馈闭环

设计原则（用户确认的漏洞纠正方案）：
- 触发信号是「结构性」的，不是「0 新增」：安静但结构好的部门绝不碰。
  * 旧选择器匹配数 < 3          → 结构坏了 → 自动重探测换选择器
  * 旧选择器匹配但垃圾标题 > 0  → 抓错了   → 只标记人工复核，不自动换
- 选择器变更全部留痕（审计日志），可回滚。
- 重探测前先识别 WAF 挑战壳页，避免对着壳页空转。
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
# 自动修复要求的最低探测置信度
REPAIR_MIN_CONFIDENCE = 0.6


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


def get_review_list() -> list:
    data = _load_audit()
    return [{'dept_id': int(k), **v} for k, v in data['review'].items()]


def get_audit_changes(limit: int = 50) -> list:
    return _load_audit()['changes'][-limit:][::-1]


def rollback_selectors(department) -> bool:
    """回滚到审计日志中该部门最近一次变更前的选择器。"""
    data = _load_audit()
    for entry in reversed(data['changes']):
        if entry.get('dept_id') == department.id and entry.get('old'):
            old = entry['old']
            for field in ('list_selector', 'title_selector', 'link_selector',
                          'date_selector', 'content_selector'):
                if field in old:
                    setattr(department, field, old[field])
            from backend.database.db import db
            db.session.commit()
            record_selector_change(department, entry['new'], old,
                                   'human', '回滚自审计日志')
            clear_review(department)
            return True
    return False


# ------------------------------------------------------------------
# 健康评估与修复
# ------------------------------------------------------------------

def is_challenge_shell(html: str) -> bool:
    """WAF 挑战壳页（瑞数 $_ts 等）或空壳，不可用于重探测。"""
    if not html:
        return True
    return '$_ts' in html[:50000] or len(html) < 2000


def _quick_stats(html: str, department):
    """用部门当前选择器快速统计：匹配数 + 垃圾标题数（只查前 10 项）。"""
    from bs4 import BeautifulSoup
    from backend.scraper.detectors.title_quality import (
        is_junk_title, extract_best_title, clean_title)

    if not department.list_selector:
        return None
    soup = BeautifulSoup(html, 'lxml')
    try:
        items = soup.select(department.list_selector)
    except Exception:
        return None
    junk = 0
    for item in items[:10]:
        el = item.select_one(department.title_selector) if department.title_selector else item
        title = ''
        if el:
            attr = (el.get('title') or el.get('data-title') or '').strip()
            title = clean_title(attr or el.get_text(strip=True))
        if is_junk_title(title):
            title = clean_title(extract_best_title(item))
        if is_junk_title(title) or not title:
            junk += 1
    return {'matched': len(items), 'junk': junk}


def evaluate_and_repair(department, html: str) -> dict:
    """抓取后评估部门选择器健康，返回处置结果。

    - 结构坏（匹配<3）且非挑战壳页 → 自动重探测，达标即换（留痕）
    - 结构坏但探测不达标 / 挑战壳页 → 标记人工复核
    - 结构好但有垃圾标题 → 只标记人工复核，不自动换（防静默漂移）
    - 健康 → 清除复核标记
    """
    stats = _quick_stats(html, department)
    if stats is None:
        return {'action': 'skip'}

    if stats['matched'] >= BROKEN_MATCH_THRESHOLD and stats['junk'] == 0:
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

    # ---- 结构坏了：自动修复 ----
    from backend.scraper.detectors.list_detector import detect_notice_list
    from backend.scraper.cms_registry import load_selector_profiles
    from backend.scraper.detectors.title_quality import is_junk_title as _is_junk

    result = detect_notice_list(html, department.list_url or '',
                                existing_profiles=load_selector_profiles())
    if (result and result['confidence'] >= REPAIR_MIN_CONFIDENCE
            and result['list_selector'] != department.list_selector
            and result.get('sample_titles')
            and sum(1 for t in result['sample_titles'] if _is_junk(t)) == 0):
        old = {f: getattr(department, f) for f in (
            'list_selector', 'title_selector', 'link_selector',
            'date_selector', 'content_selector')}
        department.list_selector = result['list_selector']
        department.title_selector = result['title_selector']
        department.link_selector = result['link_selector']
        department.date_selector = result['date_selector']
        department.content_selector = result.get('content_selector',
                                                 department.content_selector)
        from backend.database.db import db
        db.session.commit()
        record_selector_change(department, old, {
            'list_selector': department.list_selector,
            'title_selector': department.title_selector,
            'link_selector': department.link_selector,
            'date_selector': department.date_selector,
            'content_selector': department.content_selector,
        }, 'auto', f"结构损坏(匹配{stats['matched']})→重探测 conf={result['confidence']}")
        clear_review(department)
        return {'action': 'repaired', **stats}

    mark_needs_review(department,
                      f"结构损坏(匹配{stats['matched']})且重探测不达标"
                      f"(conf={result['confidence'] if result else 0})")
    return {'action': 'needs_review', **stats}
