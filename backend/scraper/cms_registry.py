"""CMS 模板注册表 — 数据驱动

将「列表页选择器模板」和「日期解析格式」从引擎代码中外置到
cms_profiles.yaml。新增一种 CMS 支持只需在该文件中加配置，
无需改动 engine.py / change_detector.py 的代码。

选择器模板用于：
- 站点发现时探测新学校的通知列表结构
- 部门未配置选择器时的回退匹配

日期格式用于：
- 解析通知列表页上的各种发布时间字符串
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
CMS_PROFILES_PATH = ROOT_DIR / 'config' / 'cms_profiles.yaml'

# 选择器模板的字段默认值（新增模板时可省略部分字段）
_SELECTOR_DEFAULTS = {
    'name': '',
    'list_selector': '',
    'title_selector': 'a',
    'link_selector': 'a',
    'date_selector': 'span',
    'content_selector': 'div.article-content, div.content, div.main, article',
}


@lru_cache(maxsize=1)
def _load_raw() -> dict:
    """读取并缓存 cms_profiles.yaml 的原始内容。"""
    if not CMS_PROFILES_PATH.exists():
        logger.warning("cms_profiles.yaml 不存在，使用空模板")
        return {}
    try:
        with open(CMS_PROFILES_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"读取 cms_profiles.yaml 失败: {e}")
        return {}


@lru_cache(maxsize=1)
def load_selector_profiles() -> list:
    """加载列表页选择器模板（按 YAML 中的优先级顺序）。

    每个模板规范化为包含全部 6 个字段的 dict（缺失字段填默认值），
    与引擎期望的 SELECTOR_PROFILES 结构兼容。
    """
    data = _load_raw()
    profiles = []
    for p in data.get('selector_profiles', []) or []:
        if not isinstance(p, dict) or not p.get('list_selector'):
            continue
        normalized = dict(_SELECTOR_DEFAULTS)
        normalized.update(p)
        profiles.append(normalized)
    if not profiles:
        logger.warning("cms_profiles.yaml 中无有效 selector_profiles，选择器探测功能将受限")
    return profiles


# 正则 flags 字符串 → re 模块常量（英文月份等需要忽略大小写）
_FLAGS_MAP = {
    'IGNORECASE': re.IGNORECASE,
    'I': re.IGNORECASE,
}


@lru_cache(maxsize=1)
def load_date_patterns() -> list:
    """加载日期解析格式，返回 [(compiled_regex, format_tag), ...]。

    format_tag 取值（见 cms_profiles.yaml 头部注释）：
    - 'year-month-day'        年/月/日
    - 'month-day-year'        月/日/年
    - 'year-month-day-concat' 年月-日（202607-08）
    - 'year-month-day-compact' 纯数字紧凑（20260719）
    - 'month-name-day-year'   英文月 日 年（Jan 19, 2026）
    - 'day-month-name-year'   日 英文月 年（19 Jul 2026）
    - 'day-month'             日/月，年份推断
    - 'month-day'             月/日，年份推断
    - 'year-month'            年/月，日缺省为 1
    """
    data = _load_raw()
    patterns = []
    for entry in data.get('date_patterns', []) or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get('pattern')
        if not raw:
            continue
        flags = _FLAGS_MAP.get((entry.get('flags') or '').upper(), 0)
        try:
            compiled = re.compile(raw, flags)
        except re.error as e:
            logger.error(f"日期正则编译失败，已跳过: {raw!r} ({e})")
            continue
        patterns.append((compiled, entry.get('format', '') or ''))
    return patterns


# 英文月份缩写 → 数字（与 dom_analyzer 原 MONTH_MAP 一致）
MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def groups_to_datetime(groups: tuple, fmt: str):
    """把日期正则的捕获组 + format 标签组装成 datetime（无效返回 None）。

    这是全项目唯一的日期组装逻辑：change_detector 与 dom_analyzer
    都经由 parse_date 调用它，不再各自维护一份。
    """
    now = datetime.now()
    try:
        if fmt == 'month-day-year':
            # "07-042026" → month, day, year
            month, day, year = int(groups[0]), int(groups[1]), int(groups[2])
            return datetime(year, month, day, tzinfo=timezone.utc)
        if fmt == 'year-month-day-concat':
            # "202607-08" → year, month, day
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            return datetime(year, month, day, tzinfo=timezone.utc)
        if fmt == 'year-month-day-compact':
            # "20260719" → year, month, day
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            return datetime(year, month, day, tzinfo=timezone.utc)
        if fmt == 'month-name-day-year':
            # "Jan 19, 2026" → (month名, day, year)
            month = MONTH_MAP.get((groups[0] or '').lower()[:3], 0)
            if not month:
                return None
            return datetime(int(groups[2]), month, int(groups[1]), tzinfo=timezone.utc)
        if fmt == 'day-month-name-year':
            # "19 Jul 2026" → (day, month名, year)
            month = MONTH_MAP.get((groups[1] or '').lower()[:3], 0)
            if not month:
                return None
            return datetime(int(groups[2]), month, int(groups[0]), tzinfo=timezone.utc)
        if fmt == 'day-month':
            # "1707月" → day, month；年份推断，若结果在未来则取去年
            day, month = int(groups[0]), int(groups[1])
            year = now.year
            dt = datetime(year, month, day, tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc):
                dt = datetime(year - 1, month, day, tzinfo=timezone.utc)
            return dt
        if fmt == 'month-day':
            # "7月8日" → month, day；年份推断，若结果在未来则取去年
            month, day = int(groups[0]), int(groups[1])
            year = now.year
            dt = datetime(year, month, day, tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc):
                dt = datetime(year - 1, month, day, tzinfo=timezone.utc)
            return dt
        if fmt == 'year-month':
            # "2026-05" → year, month；日缺省为 1
            year, month = int(groups[0]), int(groups[1])
            return datetime(year, month, 1, tzinfo=timezone.utc)
        if fmt == 'day-year-month':
            # "26 2026.08" / "29 2026/08" → 日 + 年.月（博达多校拼接格式）
            day, year, month = int(groups[0]), int(groups[1]), int(groups[2])
            return datetime(year, month, day, tzinfo=timezone.utc)
        if fmt == 'relative':
            # "9小时前" / "3天前" → 以当前时间回推
            n = int(groups[0])
            now = datetime.now(timezone.utc)
            if groups[1] == '天前':
                return now - timedelta(days=n)
            if groups[1] == '分钟前':
                return now - timedelta(minutes=n)
            return now - timedelta(hours=n)
        if len(groups) == 3:
            # 年/月/日（通用：覆盖 year-month-day、括号日期等）
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
            if year < 100:
                year += 2000
            return datetime(year, month, day, tzinfo=timezone.utc)
        if len(groups) == 2:
            # 月/日（未知 2 组格式的兜底）
            month, day = int(groups[0]), int(groups[1])
            year = now.year
            dt = datetime(year, month, day, tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc):
                dt = datetime(year - 1, month, day, tzinfo=timezone.utc)
            return dt
    except (ValueError, TypeError, IndexError):
        return None
    return None


def parse_date(date_text):
    """解析文本中的第一个日期，返回 datetime 或 None。

    遍历 cms_profiles.yaml 中定义的全部日期格式，命中即返回。
    这是全项目统一的日期解析入口。
    """
    if not date_text:
        return None

    date_text = date_text.strip()

    for pattern, fmt in load_date_patterns():
        match = pattern.search(date_text)
        if not match:
            continue
        dt = groups_to_datetime(match.groups(), fmt)
        if dt:
            return dt

    return None
