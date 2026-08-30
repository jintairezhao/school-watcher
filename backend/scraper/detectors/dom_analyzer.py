"""通用 DOM 结构分析引擎

与 CMS 无关的 HTML 结构启发式分析工具集。
不依赖任何特定 CSS 类名或 CMS 模板 — 纯 DOM 结构分析。

核心能力：
- 重复块检测（通知列表识别）
- 文本密度分析（正文区域定位）
- 导航区域识别（链接密度分析）
- 翻页控件检测
- 通用日期提取
"""

import re
import logging
from collections import Counter, defaultdict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from backend.scraper.cms_registry import parse_date, load_date_patterns, groups_to_datetime

logger = logging.getLogger(__name__)

# ---- 噪声标签（在分析前移除） ----
NOISE_TAGS = {'script', 'style', 'noscript', 'iframe', 'svg', 'canvas',
              'input', 'textarea', 'select', 'button', 'option'}
SEMANTIC_NOISE = {'nav', 'footer', 'header', 'aside'}

# 日期解析已统一到 cms_profiles.yaml（cms_registry 提供 parse_date /
# groups_to_datetime），此处不再维护独立的日期正则与月份映射。


def _dom_signature(element):
    """计算单个 DOM 元素的结构签名。

    签名包含：标签名、CSS 类名排序列表、子元素标签序列、
    是否含链接、是否含日期文本。

    用于判断两个 DOM 元素是否具有相同的"模板结构"。
    """
    if not isinstance(element, Tag):
        return None

    # 子元素标签序列
    child_tags = tuple(
        c.name for c in element.find_all(True, recursive=False)
        if c.name not in NOISE_TAGS
    ) if hasattr(element, 'find_all') else ()

    # CSS 类名（排序去重）
    classes = tuple(sorted(element.get('class', []))) if element.get('class') else ()

    # 是否含链接
    has_link = element.name == 'a' or (hasattr(element, 'find') and element.find('a') is not None)

    # 是否有日期文本
    text = element.get_text(strip=True) if hasattr(element, 'get_text') else ''
    has_date = bool(_find_first_date(text))

    return {
        'tag': element.name,
        'classes': classes,
        'child_tags': child_tags,
        'has_link': has_link,
        'has_date': has_date,
    }


def _signature_key(sig):
    """将签名转为可哈希的键，用于分组比较。"""
    if sig is None:
        return None
    return (sig['tag'], sig['classes'], sig['child_tags'])


def _has_dates_in_run(items):
    """检查重复块中是否有日期文本。"""
    date_count = 0
    for item in items:
        text = item.get_text(strip=True) if hasattr(item, 'get_text') else ''
        if _find_first_date(text):
            date_count += 1
    return date_count / max(len(items), 1)


def _has_links_in_run(items):
    """检查重复块中是否有链接。"""
    link_count = 0
    for item in items:
        if item.name == 'a' or (hasattr(item, 'find') and item.find('a') is not None):
            link_count += 1
    return link_count / max(len(items), 1)


def _css_path(element, root=None, max_depth=4):
    """为元素生成简洁的 CSS 选择器路径。"""
    if element is None:
        return ''
    parts = []
    current = element
    depth = 0
    while current is not None and current is not root and depth < max_depth:
        if not isinstance(current, Tag):
            break
        if current.get('id'):
            parts.append(f"#{current['id']}")
            break
        selector = current.name
        if current.get('class'):
            selector += '.' + '.'.join(current['class'])
        parts.append(selector)
        current = current.parent
        depth += 1
    return ' > '.join(reversed(parts))


def _find_first_date(text):
    """在文本中查找第一个日期，返回 (datetime, pattern_name) 或 None。

    日期解析统一走 cms_registry（数据来自 cms_profiles.yaml）。
    """
    if not text:
        return None
    dt = parse_date(text)
    return (dt, '') if dt else None


def find_repeating_blocks(soup, min_repeat=3, min_text_len=8):
    """找出 DOM 中重复出现的兄弟元素块（候选列表项）。

    这是通用列表检测的核心算法。不依赖任何 CSS 类名或标签名 —
    纯粹通过 DOM 结构重复模式识别列表。

    Args:
        soup: BeautifulSoup 解析后的 HTML
        min_repeat: 最少连续出现次数
        min_text_len: 每个项的最短文本长度

    Returns:
        list: 候选列表块，按分数降序排列
        [{parent_selector, item_tag, item_class, count, score,
          avg_text_len, date_ratio, link_ratio, sample_items, signature}]
    """
    # 移除噪声
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()

    candidates = []

    # 遍历所有容器元素
    container_tags = soup.find_all(['ul', 'ol', 'div', 'section', 'article',
                                     'tbody', 'table', 'dl', 'main'])
    # 同时检查 body 本身
    body = soup.find('body')
    if body:
        container_tags.append(body)

    processed_parents = set()

    for container in container_tags:
        # 跳过噪声容器
        container_id = container.get('id', '')
        container_class = ' '.join(container.get('class', []))
        if any(kw in container_id.lower() + container_class.lower()
               for kw in ('footer', 'sidebar', 'comment', 'share', 'related')):
            continue

        # 获取直接子元素（跳过纯文本和注释）
        children = []
        for child in container.find_all(True, recursive=False):
            if isinstance(child, Tag) and child.name not in NOISE_TAGS:
                text = child.get_text(strip=True)
                if len(text) >= min_text_len:
                    children.append(child)

        if len(children) < min_repeat:
            continue

        # 为每个子元素计算签名
        signatures = []
        for child in children:
            sig = _dom_signature(child)
            sig_key = _signature_key(sig)
            signatures.append((child, sig_key))

        # 检测连续相同签名的 run
        runs = []
        i = 0
        while i < len(signatures):
            child, sig_key = signatures[i]
            if sig_key is None:
                i += 1
                continue

            # 找从 i 开始的连续相同签名
            run_items = [child]
            j = i + 1
            while j < len(signatures) and signatures[j][1] == sig_key:
                run_items.append(signatures[j][0])
                j += 1

            if len(run_items) >= min_repeat:
                # 检查容器是否已处理（避免嵌套重复）
                parent_key = (id(container), sig_key)
                if parent_key not in processed_parents:
                    runs.append({
                        'container': container,
                        'items': run_items,
                        'sig_key': sig_key,
                        'start_idx': i,
                    })
                    processed_parents.add(parent_key)

            # 跳到 run 结束后继续（非重叠检测）
            i = max(i + 1, j)

        # 打分
        for run in runs:
            items = run['items']
            count = len(items)
            avg_text = sum(len(it.get_text(strip=True)) for it in items) / count
            date_ratio = _has_dates_in_run(items)
            link_ratio = _has_links_in_run(items)

            # 链接多样性：总是相同 URL 说明不是通知列表
            links = []
            for it in items:
                a_tags = it.find_all('a') if it.name != 'a' else [it]
                for a_tag in a_tags:
                    href = a_tag.get('href', '')
                    if href and not href.startswith(('#', 'javascript:')):
                        links.append(href)

            unique_link_ratio = len(set(links)) / max(len(links), 1) if links else 0

            # 分数计算：数量 × 平均文本长度 × 日期加成 × 链接加成 × 多样性
            score = count * min(avg_text, 200)  # 文本长度截断避免一篇文章炸分
            score *= (1.0 + date_ratio * 1.5)   # 有日期 +150%
            score *= (1.0 + link_ratio * 1.0)   # 有链接 +100%
            score *= (0.5 + unique_link_ratio * 0.5)  # 链接多样 +50%

            # 示例条目
            sample_items = []
            for it in items[:5]:
                a_tag = it.find('a') if it.name != 'a' else it
                title = a_tag.get_text(strip=True) if a_tag else it.get_text(strip=True)[:100]
                href = a_tag.get('href', '') if a_tag and a_tag.name == 'a' else ''
                # 从该项文本中找日期
                item_text = it.get_text(strip=True)
                date_result = _find_first_date(item_text)
                date_str = str(date_result[0])[:10] if date_result else ''

                sample_items.append({
                    'title': title[:100],
                    'url': href,
                    'date': date_str,
                })

            # 自动推导选择器
            first_item = items[0]
            item_tag = first_item.name

            candidates.append({
                'parent_selector': _css_path(run['container']),
                'item_tag': item_tag,
                'item_class': ' '.join(first_item.get('class', [])),
                'count': count,
                'score': round(score, 1),
                'avg_text_len': round(avg_text, 1),
                'date_ratio': round(date_ratio, 2),
                'link_ratio': round(link_ratio, 2),
                'unique_link_ratio': round(unique_link_ratio, 2),
                'sample_items': sample_items,
                'signature': {
                    'tag': run['sig_key'][0],
                    'child_tags': list(run['sig_key'][2]),
                },
            })

    # 按分数降序
    candidates.sort(key=lambda c: c['score'], reverse=True)
    return candidates


def find_main_content(soup):
    """通过文本密度分析找出页面主要内容区域。

    算法：
    1. 移除导航、页脚、侧边栏等噪声区域
    2. 对剩余元素计算"文本密度" = 可见文本长度 / 后代标签数
    3. 返回文本密度最高且文本总量足够大的元素

    Args:
        soup: BeautifulSoup 解析后的 HTML

    Returns:
        dict: {selector, text_length, tag_count, density, confidence}
        或 None（未找到合适的正文区域）
    """
    # 先移除噪声
    for tag_name in NOISE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # 移除语义噪声区域（但保留内容用于计算）
    noise_elements = []
    for tag_name in SEMANTIC_NOISE:
        noise_elements.extend(soup.find_all(tag_name))
    # 同时移除 class/id 含噪声关键词的元素
    for kw in ('sidebar', 'comment', 'share', 'related-post', 'recommend',
                'breadcrumb', 'toolbar', 'page-nav'):
        noise_elements.extend(soup.find_all(class_=re.compile(kw, re.I)))
        noise_elements.extend(soup.find_all(id=re.compile(kw, re.I)))

    # 直接 decompose
    for el in noise_elements:
        if el and hasattr(el, 'decompose'):
            el.decompose()

    # 候选语义标签（优先）
    semantic_selectors = [
        'article', '[role="main"]', 'main',
        'div.article-content', 'div.article', 'div.content', 'div.main',
        'div.post-content', 'div.entry-content', 'div.detail',
        'div.gp-article', 'div.news-content', 'div.info',
    ]

    best = None

    # 先尝试语义标签
    for sel in semantic_selectors:
        elements = soup.select(sel)
        for el in elements:
            text = el.get_text(strip=True)
            if len(text) < 100:
                continue
            tag_count = len(el.find_all(True))
            if tag_count == 0:
                continue
            density = len(text) / tag_count
            if best is None or density > best['density']:
                best = {
                    'selector': sel,
                    'element': el,
                    'text_length': len(text),
                    'tag_count': tag_count,
                    'density': round(density, 2),
                    'confidence': 0.7,
                    'method': 'semantic_match',
                }

    # 语义未找到 → 文本密度分析
    if best is None or best['text_length'] < 200:
        # 遍历所有可能的内容容器
        for el in soup.find_all(['div', 'section', 'article']):
            text = el.get_text(strip=True)
            if len(text) < 200:
                continue
            tag_count = len(el.find_all(True))
            if tag_count == 0:
                continue

            # 链接密度惩罚
            a_count = len(el.find_all('a'))
            a_text_len = sum(len(a.get_text(strip=True)) for a in el.find_all('a'))
            link_density = a_text_len / max(len(text), 1)
            if link_density > 0.5:
                continue  # 太多链接，可能是导航或列表

            density = len(text) / tag_count

            if best is None or (density > best['density'] and len(text) >= best.get('text_length', 0) * 0.5):
                best = {
                    'selector': _css_path(el),
                    'element': el,
                    'text_length': len(text),
                    'tag_count': tag_count,
                    'density': round(density, 2),
                    'confidence': min(0.9, density / 50),
                    'method': 'text_density',
                }

    if best:
        # 移除 element 引用（不可 JSON 序列化）
        result = {k: v for k, v in best.items() if k != 'element'}
        return result
    return None


def find_navigation_area(soup):
    """找出页面主导航区域。

    算法：链接密度分析 + 语义标签优先。

    Args:
        soup: BeautifulSoup 解析后的 HTML

    Returns:
        dict: {selector, link_count, depth, categories: [{name, url, children}]}
        或 None
    """
    # 语义标签优先
    nav_selectors = [
        'nav', '[role="navigation"]',
        '#nav', '#menu', '#navigation',
        '.nav', '.navbar', '.menu', '.navigation',
        '.header-nav', '.top-nav', '.main-nav', '.site-nav',
        '.header-menu', '.top-menu', '.main-menu',
    ]

    best_nav = None

    for sel in nav_selectors:
        elements = soup.select(sel)
        for el in elements:
            links = el.find_all('a', href=True)
            # 过滤空链接和 javascript:
            valid_links = [
                a for a in links
                if a.get('href', '').strip()
                and not a['href'].startswith('javascript:')
                and not a['href'] == '#'
            ]
            if len(valid_links) >= 4:
                # 解析导航层级
                categories = _parse_nav_tree(el)
                best_nav = {
                    'selector': sel,
                    'link_count': len(valid_links),
                    'depth': _max_depth(el),
                    'categories': categories,
                    'confidence': 0.9,
                }
                break
        if best_nav:
            break

    # 语义标签未找到 → 链接密度分析
    if not best_nav:
        candidates = []
        for el in soup.find_all(['ul', 'div', 'nav']):
            links = el.find_all('a', href=True)
            valid_links = [
                a for a in links
                if a.get('href', '').strip()
                and not a['href'].startswith('javascript:')
                and not a['href'] == '#'
            ]
            if len(valid_links) < 4:
                continue
            text = el.get_text(strip=True)
            link_text = sum(len(a.get_text(strip=True)) for a in valid_links)
            link_density = link_text / max(len(text), 1)
            if link_density < 0.3:
                continue

            candidates.append({
                'selector': _css_path(el),
                'element': el,
                'link_count': len(valid_links),
                'link_density': round(link_density, 2),
                'score': len(valid_links) * link_density,
            })

        candidates.sort(key=lambda c: c['score'], reverse=True)
        if candidates:
            best = candidates[0]
            categories = _parse_nav_tree(best['element'])
            best_nav = {
                'selector': best['selector'],
                'link_count': best['link_count'],
                'depth': _max_depth(best['element']),
                'categories': categories,
                'confidence': round(min(0.8, best['link_density']), 2),
            }

    return best_nav


def _max_depth(element, current_depth=0):
    """计算元素的最大嵌套深度。"""
    if not hasattr(element, 'find_all'):
        return current_depth
    children = element.find_all(True, recursive=False)
    if not children:
        return current_depth
    return max(_max_depth(c, current_depth + 1) for c in children)


def _parse_nav_tree(nav_element):
    """从导航元素解析层级结构。

    返回 [{name, url, children: [...]}] 格式的树。
    支持 li-based 和 div-based 两种导航结构。
    """
    categories = []

    # 找顶级 <li>（传统 ul/li 导航）
    top_items = nav_element.find_all('li', recursive=False)
    if not top_items:
        # <li> 可能嵌套更深
        top_items = nav_element.find_all('li', recursive=True)
        # 过滤: 只保留直接子 <li>（排除嵌套在子菜单中的）
        top_items = [li for li in top_items
                     if li.parent == nav_element or li.parent.parent == nav_element]

    if not top_items:
        # li-based 导航未找到 → 尝试 div-based 导航（如 PKU）
        # 查找包含链接的直接子 div 或 section
        for child in nav_element.find_all(True, recursive=False):
            if child.name in NOISE_TAGS:
                continue
            links = child.find_all('a', href=True)
            if len(links) >= 2:
                # 以这个 div 作为一级分类
                # 提取其中的第一层链接
                first_level = []
                for a in child.find_all('a', href=True, recursive=False):
                    name = a.get_text(strip=True)
                    url = a.get('href', '').strip()
                    if name and len(name) >= 2 and not url.startswith('javascript:'):
                        first_level.append(a)

                if not first_level:
                    # 链接不在直接子节点，更深一层
                    for sub in child.find_all(True, recursive=False):
                        for a in sub.find_all('a', href=True, recursive=False):
                            name = a.get_text(strip=True)
                            url = a.get('href', '').strip()
                            if name and len(name) >= 2 and not url.startswith('javascript:'):
                                first_level.append(a)

                for a in first_level[:30]:
                    name = a.get_text(strip=True)
                    url = a.get('href', '').strip()
                    if name and len(name) >= 2:
                        categories.append({
                            'name': name,
                            'url': url,
                            'children': [],
                            'has_children': False,
                        })

                if len(categories) >= 4:
                    break  # 取第一个有足够链接的容器

        return categories

    # li-based 导航处理（原有逻辑）
    for li in top_items[:40]:  # 最多 40 个顶级项
        a_tag = li.find('a', href=True, recursive=False)
        if not a_tag:
            # 可能在更深层
            a_tag = li.find('a', href=True)

        if not a_tag:
            continue

        name = a_tag.get_text(strip=True)
        url = a_tag.get('href', '')

        if not name or len(name) < 2:
            continue

        # 查找子菜单
        children = []
        submenu = li.find(['ul', 'ol', 'div'], recursive=False)
        if submenu:
            sub_links = submenu.find_all('a', href=True)
            for sub_a in sub_links[:20]:
                sub_name = sub_a.get_text(strip=True)
                sub_url = sub_a.get('href', '')
                if sub_name and len(sub_name) >= 2:
                    children.append({
                        'name': sub_name,
                        'url': sub_url,
                    })

        categories.append({
            'name': name,
            'url': url,
            'children': children,
            'has_children': len(children) > 0,
        })

    return categories


def find_pagination_links(soup, current_url):
    """检测页面翻页链接。

    算法：
    1. 查找 class/id 含 'page'/'pagin' 的元素
    2. 分析其中的数字链接序列
    3. 推断翻页 URL 模板

    Args:
        soup: BeautifulSoup 解析后的 HTML
        current_url: 当前页面 URL

    Returns:
        dict: {style, page_param, max_page, sample_urls, confidence}
        或 None
    """
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

    # 找翻页容器
    page_selectors = [
        '.pages', '.pagination', '.pager', '.page-nav',
        '.paging', '.pagelist', '[class*="page"]',
        '.fenye', '.page_div', '#page',
    ]

    page_element = None
    for sel in page_selectors:
        try:
            page_element = soup.select_one(sel)
            if page_element:
                break
        except Exception:
            continue

    if not page_element:
        # 全页搜索含数字序列的链接
        all_a = soup.find_all('a', href=True)
        # 找连续的页码链接
        for a in all_a:
            if a.get_text(strip=True).isdigit():
                parent = a.parent
                siblings = parent.find_all('a', href=True)
                digits = [sib.get_text(strip=True)
                          for sib in siblings
                          if sib.get_text(strip=True).isdigit()]
                if len(digits) >= 2:
                    page_element = parent
                    break

    if not page_element:
        return None

    # 提取所有含数字文本的链接
    page_links = []
    for a in page_element.find_all('a', href=True):
        text = a.get_text(strip=True)
        href = a['href'].strip()
        if text.isdigit() and href:
            page_links.append({
                'page_num': int(text),
                'url': href,
            })

    if len(page_links) < 2:
        return None

    page_links.sort(key=lambda p: p['page_num'])
    max_page = page_links[-1]['page_num']

    # 分析 URL 模式
    sample_url = page_links[1]['url']  # 第2个（对应某页码）

    # 全 URL 还是相对路径
    if not sample_url.startswith('http'):
        sample_url = urljoin(current_url, sample_url)

    parsed = urlparse(sample_url)

    # 检测翻页格式
    style = None
    page_param = None

    # 路径格式: /index_N.shtml, /index_N.htm, /indexN.htm
    path = parsed.path
    for fmt_name, regex in [
        ('index_shtml', r'(/index)_(\d+)\.shtml$'),
        ('index_htm', r'(/index)_(\d+)\.htm$'),
        ('index_htm_offset', r'(/index)(\d+)\.htm$'),  # 零偏移
        ('page_path', r'(/page)/(\d+)/?$'),
        ('list_htm', r'(/list)_(\d+)\.htm'),
        ('default_htm', r'/(\d+)\.htm$'),
    ]:
        m = re.search(regex, path)
        if m:
            # zero-offset: index1.htm means page 2
            if fmt_name == 'index_htm_offset':
                style = 'index_htm_offset'
                page_param = m.group(1)
            elif fmt_name == 'default_htm':
                style = 'default_htm'
                page_param = '/{page}.htm'
            else:
                style = 'index_htm' if 'htm' in fmt_name else 'default'
                page_param = m.group(1)
            break

    # Query string 格式: ?page=N 或任何包含数字的参数
    if not style and parsed.query:
        params = parse_qs(parsed.query)
        for key, values in params.items():
            if len(values) == 1 and values[0].isdigit():
                style = 'query_page'
                page_param = key
                break

    if style:
        return {
            'style': style,
            'page_param': page_param,
            'max_page': max_page,
            'sample_urls': [p['url'] for p in page_links[:3]],
            'confidence': 0.7,
        }

    return None


def extract_dates_from_text(text):
    """从任意文本中提取所有可能的日期。

    日期解析统一走 cms_registry（数据来自 cms_profiles.yaml）。

    Args:
        text: 任意文本字符串

    Returns:
        list: [{datetime, pattern, raw, reliability}, ...]
        按可靠性排序（年月日完整 > 只有月日）
    """
    if not text:
        return []

    results = []

    for pattern, fmt in load_date_patterns():
        for match in pattern.finditer(text):
            groups = match.groups()
            dt = groups_to_datetime(groups, fmt)
            if dt:
                # 可靠性评分
                if len(groups) == 3 and all(
                    isinstance(g, str) and g.isdigit()
                    for g in groups
                ):
                    reliability = 3  # 年-月-日 完整
                elif len(groups) == 2:
                    reliability = 1  # 只有月日
                else:
                    reliability = 2

                results.append({
                    'datetime': dt,
                    'pattern': pattern.pattern[:40],
                    'raw': match.group(0),
                    'reliability': reliability,
                })

    # 按可靠性降序、日期降序
    results.sort(key=lambda r: (r['reliability'], r['datetime']), reverse=True)
    return results


def find_notice_list_page_links(soup, base_url):
    """从部门首页找通知列表子页面的链接。

    扫描页面中所有链接，找出 href 含通知关键词的链接。
    优先级：路径短 > 路径长（优先索引页而非具体文章页）。

    Args:
        soup: 部门首页的 BeautifulSoup
        base_url: 基准 URL

    Returns:
        list: [{url, text, score, is_index}] 按分数降序
    """
    NOTICE_KEYWORDS = [
        'tzgg', 'notice', 'tongzhi', 'gonggao', 'announcement',
        'news', 'xwdt', 'xwzx', 'info', 'article', 'list',
        'xinwen', 'tongzhigonggao', 'ggl', 'tzg', 'xytz',
    ]

    candidates = []

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href'].strip()
        text = a_tag.get_text(strip=True)

        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue

        # 检查文本和 href 是否含通知关键词
        href_lower = href.lower()
        text_lower = text.lower()

        kw_score = 0
        for k, kw in enumerate(NOTICE_KEYWORDS):
            if kw in href_lower:
                kw_score += 3
            if kw in text_lower:
                kw_score += 2

        if kw_score == 0:
            continue

        # 索引页判定：URL 以 / 或 index 结尾
        is_index = bool(
            href_lower.endswith('/')
            or 'index' in href_lower
            or re.search(r'/tzgg\d*/*$', href_lower)
            or re.search(r'/notice\d*/*$', href_lower)
            or re.search(r'/news\d*/*$', href_lower)
        )

        # 分数：关键词 + 索引页加成 + 短路径加成
        score = kw_score
        if is_index:
            score += 5
        score -= len(href) * 0.02  # 路径越长扣分越多

        # 排除可能的文章页（URL 含长数字或 .htm/.shtml 且不是 index）
        if not is_index and re.search(r'/\d{4,}|content_\d+|art_\d+', href_lower):
            score -= 3

        full_url = urljoin(base_url, href)
        candidates.append({
            'url': full_url,
            'text': text[:50],
            'score': round(score, 1),
            'is_index': is_index,
        })

    # 去重
    seen_urls = set()
    unique = []
    for c in sorted(candidates, key=lambda x: x['score'], reverse=True):
        if c['url'] not in seen_urls:
            seen_urls.add(c['url'])
            unique.append(c)

    return unique[:10]


def is_likely_notice_list(candidate):
    """判断检测到的重复块是否像一个通知列表。

    返回 (bool, str) — (是否像, 原因)。
    """
    if candidate['count'] < 5:
        return False, f"条目太少 ({candidate['count']} < 5)"

    if candidate['link_ratio'] < 0.5:
        return False, f"链接比例太低 ({candidate['link_ratio']:.0%})"

    if candidate['date_ratio'] < 0.3:
        return False, f"日期比例太低 ({candidate['date_ratio']:.0%})"

    if candidate['unique_link_ratio'] < 0.5:
        return False, "链接多样性不足（可能都是相同URL）"

    # 检查样本标题是否像通知（不是导航链接）
    nav_keywords = {'首页', '下一页', '上一页', '末页', '登录', '注册',
                     '更多', '返回', 'English', '中文', '网站地图'}
    non_nav_count = 0
    for item in candidate['sample_items']:
        title = item['title']
        if title and not any(kw in title for kw in nav_keywords) and len(title) >= 4:
            non_nav_count += 1

    if non_nav_count < 3:
        return False, "样本标题像导航而非通知"

    return True, "OK"
