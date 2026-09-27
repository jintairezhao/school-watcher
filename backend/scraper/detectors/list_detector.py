"""通用通知列表检测器

利用 DOM 结构分析自动检测页面中的通知列表，
自动推导 CSS 选择器，不依赖任何硬编码的 CMS 模板。
"""

import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from backend.scraper.detectors.dom_analyzer import (
    find_repeating_blocks, extract_dates_from_text,
    is_likely_notice_list, _find_first_date, _css_path,
)
from backend.scraper.detectors.title_quality import is_junk_title, clean_title
from backend.scraper.change_detector import parse_date

logger = logging.getLogger(__name__)


def detect_notice_list(html, url, existing_profiles=None):
    """检测页面中的通知列表。

    策略：
    1. 优先匹配已知 SELECTOR_PROFILES（精确+快速）
    2. 未命中 → DOM 结构分析
    3. 分析结果中优选有日期+链接+合理标题的候选

    Args:
        html: 页面 HTML 字符串
        url: 页面 URL（用于相对链接解析和日志）
        existing_profiles: 已知的 SELECTOR_PROFILES 列表（可选）

    Returns:
        dict: {
            'list_selector': str,      # 列表容器 CSS 选择器
            'title_selector': str,     # 标题 CSS 选择器
            'link_selector': str,      # 链接 CSS 选择器
            'date_selector': str,      # 日期 CSS 选择器
            'content_selector': str,   # 建议的正文选择器
            'confidence': float,       # 置信度 0-1
            'item_count': int,         # 检测到的条目数
            'sample_titles': [str],    # 示例标题
            'method': str,             # 检测方法: 'profile_match' | 'dom_analysis'
        }
        或 None（完全失败）
    """
    soup = BeautifulSoup(html, 'lxml')

    # ---- 策略 1: 已知 PROFILE 打分制匹配 ----
    # 全量评估取最优（valid 数 → 选择器专用度），避免宽模板「先命中先返回」
    # 遮蔽更精确的 CMS 模板
    if existing_profiles:
        best = None  # (valid, specificity, profile, items, sample_titles)
        for profile in existing_profiles:
            try:
                items = soup.select(profile['list_selector'])
            except Exception:
                continue
            if len(items) < 3:
                continue
            # 验证：至少有一个条目的标题看起来合理
            valid_items = 0
            sample_titles = []
            for item in items[:10]:
                title_el = item.select_one(profile['title_selector'])
                if title_el:
                    # 苏迪等 CMS 标题在 a[title]/data-title 属性里，文本为空
                    title = clean_title(
                        title_el.get_text(strip=True)
                        or (title_el.get('title') or '').strip()
                        or (title_el.get('data-title') or '').strip())
                    # 质量门控：纯日期/MORE 等垃圾标题不算有效条目
                    link_el = item.select_one(profile.get('link_selector') or 'a[href]')
                    date_el = item.select_one(profile['date_selector']) if profile.get('date_selector') else None
                    dated = parse_date(date_el.get_text(strip=True)) if date_el is not None else None
                    article_url = urljoin(url, link_el.get('href', '')) if link_el and dated else ''
                    if len(title) >= 4 and not is_junk_title(title, article_url=article_url):
                        valid_items += 1
                        sample_titles.append(title[:100])

            if valid_items < 3:
                continue
            # 专用度：class/id/:has 越多越像精确 CMS 模板（通用回退排最后）
            sel = profile['list_selector']
            specificity = sel.count('.') + sel.count(':') + sel.count('#')
            if best is None or (valid_items, specificity) > (best[0], best[1]):
                best = (valid_items, specificity, profile, items, sample_titles)

        if best:
            valid_items, _, profile, items, sample_titles = best
            return {
                'list_selector': profile['list_selector'],
                'title_selector': profile['title_selector'],
                'link_selector': profile.get('link_selector', 'a'),
                'date_selector': profile.get('date_selector', ''),
                'content_selector': profile.get('content_selector',
                                                'div.article-content, div.content, div.main, article'),
                'confidence': 0.95,
                'item_count': len(items),
                'sample_titles': sample_titles[:5],
                'method': 'profile_match',
                'profile_name': profile.get('name', 'unknown'),
            }

    # ---- 策略 2: DOM 结构分析 ----
    candidates = find_repeating_blocks(soup, min_repeat=3, min_text_len=8)

    if not candidates:
        return None

    # 对每个候选进行通知列表特征评估
    scored_candidates = []
    for c in candidates:
        is_likely, reason = is_likely_notice_list(c)
        if not is_likely:
            continue

        # 标题质量门控：样本标题过半为垃圾（纯日期/MORE/导航文本）的候选
        # 直接淘汰——选它必然抓回一堆垃圾标题
        sample_titles = [s.get('title', '') for s in c['sample_items'][:5]]
        ok_ratio = (sum(1 for t in sample_titles if not is_junk_title(t))
                    / len(sample_titles)) if sample_titles else 0.0
        if ok_ratio < 0.5:
            continue

        # 额外加分：检测到的日期是否在合理范围
        date_bonus = 0
        for item in c['sample_items']:
            if item['date']:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(item['date'])
                    if dt.year >= 2020:
                        date_bonus += 0.1
                except (ValueError, TypeError):
                    pass

        total_score = c['score'] * (1 + date_bonus) * (0.5 + ok_ratio * 0.5)
        scored_candidates.append((total_score, c, reason))

    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, best, reason = scored_candidates[0]

    # ---- 从最佳候选中自动推导选择器 ----
    item_tag = best['item_tag']
    item_class = best['item_class']

    # 列表选择器
    list_selector = best['parent_selector'] + ' > ' + item_tag
    if item_class:
        list_selector += '.' + item_class.replace(' ', '.')

    # 标题和链接选择器（通常是 <a> 标签）
    title_selector = 'a'
    link_selector = 'a'

    # 日期选择器：从样本条目中分析 DOM 结构
    date_selector = _derive_date_selector(soup, best)

    # 内容选择器默认（后续可由 content_extractor 细化）
    content_selector = 'div.article-content, div.content, div.main, div.article, article'

    # 置信度：基于多重信号的加权
    confidence = min(0.9, (
        best['date_ratio'] * 0.35
        + best['link_ratio'] * 0.25
        + best['unique_link_ratio'] * 0.20
        + min(best['count'] / 20, 1.0) * 0.10
        + min(len(best['sample_items'][0]['title']) / 40, 1.0) * 0.10
    )) if best['sample_items'] else 0.5

    result = {
        'list_selector': list_selector,
        'title_selector': title_selector,
        'link_selector': link_selector,
        'date_selector': date_selector,
        'content_selector': content_selector,
        'confidence': round(confidence, 2),
        'item_count': best['count'],
        'sample_titles': [s['title'][:100] for s in best['sample_items'][:5]],
        'method': 'dom_analysis',
        'reason': reason,
    }

    # 如果有备选方案，也附上
    if len(scored_candidates) > 1:
        alt = scored_candidates[1][1]
        result['alternatives'] = [{
            'list_selector': alt['parent_selector'] + ' > ' + alt['item_tag'],
            'item_count': alt['count'],
            'score': alt['score'],
            'sample_titles': [s['title'][:60] for s in alt['sample_items'][:3]],
        }]

    return result


def _derive_date_selector(soup, candidate):
    """从样本条目分析日期元素的选择器。

    分析列表项中哪个子元素最可能包含日期。
    """
    # 从页面重新获取样本来分析子元素
    parent = soup.select_one(candidate['parent_selector'])
    if not parent:
        return 'span, em, i, time'

    items = parent.find_all(candidate['item_tag'], recursive=False)
    if not items:
        # 尝试 class 匹配
        if candidate['item_class']:
            cls = candidate['item_class'].split()[0]
            items = parent.find_all(candidate['item_tag'], class_=cls, recursive=False)
    if not items:
        items = parent.find_all(candidate['item_tag'], recursive=False)[:10]

    # 对每个 item 的子元素分析哪个最像日期容器
    date_tag_counts = {}
    for item in items[:5]:
        for child in item.find_all(True, recursive=False):
            if child.name in ('a', 'img', 'br', 'hr', 'script', 'style'):
                continue
            text = child.get_text(strip=True)
            if _find_first_date(text):
                # 给这个标签+class组合投票
                key = child.name
                if child.get('class'):
                    key += '.' + '.'.join(child.get('class')[:2])
                date_tag_counts[key] = date_tag_counts.get(key, 0) + 1

    if date_tag_counts:
        # 返回最常见的 2 个
        top = sorted(date_tag_counts.items(), key=lambda x: x[1], reverse=True)[:2]
        return ', '.join(t[0] for t in top)

    # 回退：常见日期标签
    return 'span, em, i, time'


def test_selectors(html, url, list_selector, title_selector='a',
                   link_selector='a', date_selector='span'):
    """测试一组选择器，返回匹配结果。

    用于用户在前端测试选择器时的实时预览。

    Args:
        html: 页面 HTML
        url: 页面 URL
        list_selector: 列表容器选择器
        title_selector: 标题选择器
        link_selector: 链接选择器
        date_selector: 日期选择器

    Returns:
        dict: {item_count, items: [{title, url, date}], warnings: [str]}
    """
    soup = BeautifulSoup(html, 'lxml')

    items = soup.select(list_selector)
    if not items:
        return {
            'item_count': 0,
            'items': [],
            'warnings': [f'选择器 "{list_selector}" 未匹配任何元素'],
        }

    results = []
    warnings = []

    for item in items[:10]:
        title = ''
        item_url = ''

        # 标题
        if title_selector:
            title_el = item.select_one(title_selector)
            if title_el:
                title = title_el.get_text(strip=True)

        # 链接
        if link_selector:
            link_el = item.select_one(link_selector)
            if link_el and link_el.name == 'a':
                href = link_el.get('href', '')
                item_url = urljoin(url, href) if href else ''

        # 日期
        date_str = ''
        if date_selector:
            date_el = item.select_one(date_selector)
            if date_el:
                date_text = date_el.get_text(strip=True)
                date_result = _find_first_date(date_text)
                if date_result:
                    date_str = str(date_result[0])[:10] if date_result[0] else date_text

        results.append({
            'title': title[:200] if title else '(无标题)',
            'url': item_url,
            'date': date_str,
        })

    if len(items) == 1:
        warnings.append('只有 1 条匹配，可能选择器不够精确')
    elif len(items) > 50:
        warnings.append(f'匹配了 {len(items)} 条，可能包含非通知内容')

    if results and not any(r['title'] and r['title'] != '(无标题)' for r in results):
        warnings.append('所有条目标题为空，title_selector 可能不正确')
    elif results and sum(1 for r in results if is_junk_title(r['title'])) >= max(1, len(results) // 2):
        warnings.append('多数标题为日期/导航文本，title_selector 抓错了节点')

    return {
        'item_count': len(items),
        'items': results,
        'warnings': warnings,
    }
