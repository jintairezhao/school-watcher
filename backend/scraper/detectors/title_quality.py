"""标题质量门控（通用，不依赖任何学校/CMS 特判）

站点列表页的「标题」节点经常被误判为日期 span、MORE 链接等，
导致入库标题是纯日期或导航文本。本模块提供：

- is_junk_title: 判定一个字符串是否不可能作为通知标题
- extract_best_title: 在列表项内按回退链挑选最佳真实标题
- date_from_url: 日期节点缺失时从文章 URL 推断发布日期

所有学校的抓取/发现流程共用这套门控，新学校自动受益。
"""

import re
from datetime import datetime, timezone

# 纯日期形态（整串匹配才判垃圾，「2026年8月28日关于…的通知」这类真实标题不受影响）
_DATE_ONLY_RES = [
    re.compile(r'^(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$'),
    re.compile(r'^(20\d{2})[-/.](\d{1,2})$'),
    re.compile(r'^(20\d{2})年(\d{1,2})月$'),
    re.compile(r'^\d{1,2}-\d{1,2}$'),
    re.compile(r'^\d{1,2}\.\d{1,2}$'),
    re.compile(r'^\d{8}$'),
    # 英文日期（HUST 等站点：Aug 14, 2026 / 14 Aug 2026）
    re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}$', re.I),
    re.compile(r'^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4}$', re.I),
]

# 短标题必须含这些强指示词才算真标题；否则视为栏目/导航标签
# （「医院概况」「课程及考试安排」这类栏目名不含这些词）
_SHORT_INDICATORS = ('关于', '通知', '公告', '公示', '通报', '名单', '结果',
                     '决定', '办法', '意见', '召开', '举行', '开展',
                     '做好', '申报', '推荐', '评选', '征集', '报名', '放假')

# 导航/占位文本（小写比较）
_JUNK_EXACT = {
    'more', 'more>>', 'more »', '>>', '»', '[详细]', '详细', '详细>',
    '更多', '首页', '查看详情', '查看全文', '查看更多', '进入', '正文',
    '当前位置', '上一篇', '下一篇', '返回', 'close', '附件', '附件下载',
}

# 文章 URL 中的日期（/2026-08-28/ 分隔形态，或上交式 /20260828/ 紧凑形态）
_URL_DATE_RE = re.compile(r'/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})/')
_URL_DATE_COMPACT_RE = re.compile(r'/(20\d{2})(\d{2})(\d{2})/')


def is_junk_title(title) -> bool:
    """True 表示该字符串不可能是一条通知的标题。"""
    if not title:
        return True
    t = title.strip()
    if len(t) < 4:
        return True
    if t.lower() in _JUNK_EXACT:
        return True
    if any(r.match(t) for r in _DATE_ONLY_RES):
        return True
    # 短字符串且无强指示词 → 栏目/导航标签而非通知标题
    if len(t) <= 8 and not any(w in t for w in _SHORT_INDICATORS):
        return True
    return False


def _title_score(t: str) -> int:
    """启发式标题得分：长度适中 + CJK 字符多更像真实标题。"""
    score = 0
    if 8 <= len(t) <= 80:
        score += 10
    score += sum(2 for ch in t if '一' <= ch <= '鿿')
    if any(p in t for p in ('关于', '通知', '公告', '公示', '通报', '决定', '意见', '办法')):
        score += 6
    return score


def extract_best_title(item) -> str:
    """在列表项元素内按回退链挑选最佳真实标题，找不到返回 ''。

    回退链：a[title] 属性 → 各链接文本得分最高且非垃圾者 →
    子级文本节点（标题常在链接的兄弟 span 里，或链接纯为日期/图片）。
    日期链接/MORE 链接因得分低或被门控而自然落选。
    """
    best, best_score = '', -1
    for a in item.find_all('a', href=True):
        cand = (a.get('title') or a.get('data-title') or '').strip() or a.get_text(strip=True)
        if is_junk_title(cand):
            continue
        s = _title_score(cand)
        if s > best_score:
            best, best_score = cand, s
    if best:
        return best
    # 链接全为垃圾（日期/图片链接）时，在子级文本节点里找最佳标题
    for el in item.find_all(['span', 'div', 'p', 'h1', 'h2', 'h3', 'h4', 'em', 'strong']):
        cand = el.get_text(strip=True)
        if is_junk_title(cand):
            continue
        s = _title_score(cand)
        if s > best_score:
            best, best_score = cand, s
    return best


# 标题开头的日期/分隔符串（博达多校把日期拼在标题文本前）
_LEAD_DATE_RE = re.compile(r'^[\d\-/.年月日\s｜|]+')


def clean_title(title: str) -> str:
    """剥离标题前缀的日期串和「浏览数：N」等噪声（通用，不分学校）。

    仅当开头串含 ≥4 个数字且带分隔符时才剥离，避免误伤
    「2026年工作要点」这类合法数字开头标题。
    """
    if not title:
        return title
    m = _LEAD_DATE_RE.match(title)
    if m and sum(ch.isdigit() for ch in m.group(0)) >= 4 \
            and any(c in m.group(0) for c in '-/.'):
        title = title[m.end():]
    title = re.sub(r'浏览数[：:]*\s*\d*', '', title)
    return title.strip(' ｜|:：-—\t')


def date_from_url(url):
    """从文章 URL 推断发布日期（日期节点缺失/解析失败时的兜底）。"""
    if not url:
        return None
    m = _URL_DATE_RE.search(url)
    groups = m.groups() if m else None
    if not groups:
        mc = _URL_DATE_COMPACT_RE.search(url)
        groups = mc.groups() if mc else None
    if not groups:
        return None
    try:
        return datetime(int(groups[0]), int(groups[1]), int(groups[2]),
                        tzinfo=timezone.utc)
    except ValueError:
        return None
