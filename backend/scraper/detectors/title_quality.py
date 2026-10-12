"""标题质量门控（通用，不依赖任何学校/CMS 特判）

站点列表页的「标题」节点经常被误判为日期 span、MORE 链接等，
导致入库标题是纯日期或导航文本。本模块提供：

- is_junk_title: 判定一个字符串是否不可能作为通知标题
- extract_best_title: 在列表项内按回退链挑选最佳真实标题
- date_from_url: 日期节点缺失时从文章 URL 推断发布日期

所有学校的抓取/发现流程共用这套门控，新学校自动受益。
"""

import re
from copy import copy
from datetime import datetime, timezone
from urllib.parse import urljoin
from backend.scraper.article_urls import ARTICLE

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
    'english', 'english version', 'english website', '中文', '中文版', '网站地图',
    '通知公告', '新闻中心', '学校新闻', '信息公开', '组织机构', '招生就业',
    '科学研究', '人才培养', '本科教育', '研究生教育',
}

# 文章 URL 中的日期（/2026-08-28/ 分隔形态，或上交式 /20260828/ 紧凑形态）
_URL_DATE_RE = re.compile(r'/(20\d{2})[-/](\d{1,2})[-/](\d{1,2})/')
_URL_DATE_COMPACT_RE = re.compile(r'/(20\d{2})(\d{2})(\d{2})/')


def is_junk_title(title, *, article_url='') -> bool:
    """True 表示该字符串不可能是一条通知的标题。"""
    if not title:
        return True
    t = title.strip()
    if len(t) < 4:
        return True
    if t.lower() in _JUNK_EXACT:
        return True
    if re.match(r'^(english\s+(version|website)|网站地图|版权所有)\b', t, re.I):
        return True
    if any(r.match(t) for r in _DATE_ONLY_RES):
        return True
    # 短字符串且无强指示词 → 栏目/导航标签而非通知标题
    if (len(t) <= 8 and not any(w in t for w in _SHORT_INDICATORS)
            and not ARTICLE.search(article_url or '')):
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


_HEADLINE = '[itemprop="headline"], .title, .headline, .news-title, .article-title, h1, h2, h3, h4, h5, h6'
_METADATA = re.compile(r'^(?:time|date|datetime|calendar|address|venue|location|person|author|speaker|tag|tags|summary|description|views|hits)(?:[-_].*)?$', re.I)


def _same_link(first, second, page_url=''):
    from backend.services.source_inventory import canonical_url
    try:
        base = urljoin('https://title-evidence.invalid/', page_url)
        left, right = canonical_url(urljoin(base, first)), canonical_url(urljoin(base, second))
        return bool(left and right and left == right)
    except (ValueError, UnicodeError):
        return False


def _belongs(node, anchor, page_url):
    owner = node if node.name == 'a' else node.find_parent('a', href=True)
    return (not owner or not anchor or owner is anchor
            or _same_link(owner.get('href', ''), anchor.get('href', ''), page_url))


def extract_title_text(element, *, anchor=None, page_url=''):
    """Read a literal headline field, excluding a card's separate metadata.

    Never remove arbitrary numeric prefixes: years, ranges and numbers can be
    real headlines. Multiple different headline fields are ambiguous evidence.
    """
    if element is None or not _belongs(element, anchor, page_url):
        return ''
    attribute = (element.get('title') or element.get('data-title') or '').strip()
    if attribute:
        return attribute
    fields = [element] if (element.name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
        or element.get('itemprop') == 'headline'
        or set(element.get('class', [])) & {'title', 'headline', 'news-title', 'article-title'}) else element.select(_HEADLINE)
    candidates = {node.get_text(' ', strip=True) for node in fields
                  if _belongs(node, anchor, page_url) and node.get_text(' ', strip=True)}
    if candidates:
        return next(iter(candidates)) if len(candidates) == 1 else ''
    # A configured wrapper may contain a date and a plain text headline. Read
    # its remaining DOM text after removing whole observed metadata fields.
    fragment = copy(element)
    for node in list(fragment.find_all(True)):
        if node.parent is None:
            continue
        tokens = [node.get('id', ''), *node.get('class', [])]
        foreign_link = (node.name == 'a' and anchor is not None
            and not _same_link(node.get('href', ''), anchor.get('href', ''), page_url))
        if (node.name in ('time', 'script', 'style') or node.get('itemprop') in ('datePublished', 'author')
                or any(_METADATA.fullmatch(token) for token in tokens) or foreign_link):
            node.decompose()
    return clean_title(fragment.get_text(' ', strip=True))


def observed_title_variants(element, title, *, anchor=None, page_url=''):
    """Exact corrupt predecessors witnessed in this card; never guess old text.

    Used only to repair an existing primary-source title for the same URL. An
    ordinary newly worded title or a different publication stays untouched.
    """
    if element is None or extract_title_text(element, anchor=anchor, page_url=page_url) != title:
        return ()
    raw = {element.get_text(strip=True), element.get_text(' ', strip=True)}
    variants = set(raw)
    for text in raw:
        variants.add(clean_title(text))
        # Historical pre-2.11 cleaner consumed a run of date/numeric characters.
        prefix = re.match(r'^[\d\-/.年月日\s｜|]+', text)
        legacy = text
        if (prefix and sum(ch.isdigit() for ch in prefix.group()) >= 4
                and any(ch in prefix.group() for ch in '-/.')):
            legacy = text[prefix.end():]
        legacy = re.sub(r'浏览数[：:]*\s*\d*', '', legacy).strip(' ｜|:：-—\t')
        variants.add(legacy)
    return tuple(value for value in variants if value and value != title)


def extract_best_title(item, *, article_url='', page_url='') -> str:
    """在列表项元素内按回退链挑选最佳真实标题，找不到返回 ''。

    回退链：a[title] 属性 → 各链接文本得分最高且非垃圾者 →
    子级文本节点（标题常在链接的兄弟 span 里，或链接纯为日期/图片）。
    日期链接/MORE 链接因得分低或被门控而自然落选。
    """
    best, best_score = '', -1
    links = item.find_all('a', href=True)
    if item.name == 'a' and item.get('href'):
        links = [item, *links]
    relevant = [a for a in links if not article_url or _same_link(a['href'], article_url, page_url or article_url)]
    if article_url and links and not relevant:
        return ''
    for a in relevant:
        cand = extract_title_text(a, anchor=a, page_url=page_url)
        if not cand and len({n.get_text(' ', strip=True) for n in a.select(_HEADLINE)
                             if n.get_text(' ', strip=True)}) > 1:
            return ''
        if is_junk_title(cand, article_url=a.get('href', '') if article_url else ''):
            continue
        s = _title_score(cand)
        if s > best_score:
            best, best_score = cand, s
    if best:
        return best
    # 链接全为垃圾（日期/图片链接）时，在子级文本节点里找最佳标题
    for el in item.find_all(['span', 'div', 'p', 'h1', 'h2', 'h3', 'h4', 'em', 'strong']):
        if not _belongs(el, relevant[0] if relevant else None, page_url):
            continue
        if el.select('a[href]'):
            continue
        if any(_METADATA.fullmatch(token) for token in [el.get('id', ''), *el.get('class', [])]):
            continue
        cand = extract_title_text(el, anchor=relevant[0] if relevant else None, page_url=page_url)
        if is_junk_title(cand, article_url=article_url):
            continue
        s = _title_score(cand)
        if s > best_score:
            best, best_score = cand, s
    return best


# Consume one calendar field, never an unrestricted run of numbers. A link
# can wrap "06-15", "2026" and the actual "2026年…" title in separate nodes.
_LEAD_DATE_RE = re.compile(
    r'^[\s｜|]*(?:'
    r'(?P<year_first>[12]\d{3})[-/.](?P<month_first>\d{1,2})[-/.](?P<day_first>\d{1,2})'
    r'|(?P<month_last>\d{1,2})[-/.](?P<day_last>\d{1,2})\s*(?P<year_last>[12]\d{3})'
    r'(?!(?:[年届级]|学年|[-/—–.][12]\d{3}))'
    r'|(?P<month_only>\d{1,2})[-/.](?P<day_only>\d{1,2})'
    r'(?:(?!\d)|(?=[12]\d{3}(?:[年届级]|学年|[-/—–.][12]\d{3}))))')


def clean_title(title: str) -> str:
    """剥离标题前缀的日期串和「浏览数：N」等噪声（通用，不分学校）。

    只剥离一段有效日期，保留随后真实标题中的年份和学年范围。
    """
    if not title:
        return title
    m = _LEAD_DATE_RE.match(title)
    if m:
        year = m.group('year_first') or m.group('year_last') or '2000'
        month = m.group('month_first') or m.group('month_last') or m.group('month_only')
        day = m.group('day_first') or m.group('day_last') or m.group('day_only')
        try:
            datetime(int(year), int(month), int(day))
        except ValueError:
            pass
        else:
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
