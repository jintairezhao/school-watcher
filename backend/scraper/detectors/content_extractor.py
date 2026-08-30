"""通用正文提取器

从任意通知详情页自动提取正文内容。
使用多重策略：语义标签 → 文本密度分析 → 回退。
"""

import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from backend.scraper.detectors.dom_analyzer import find_main_content

logger = logging.getLogger(__name__)

# 常见正文容器选择器（按优先级）
CONTENT_SELECTORS = [
    'article',
    '[role="main"]',
    'main',
    'div.article-content', 'div.article-body', 'div.article',
    'div.content', 'div.main-content', 'div.main',
    'div.post-content', 'div.entry-content',
    'div.detail', 'div.detail-content', 'div.info',
    'div.gp-article', 'div.news-content', 'div.news-body',
    'div.art-content', 'div.art-body',
    '#article', '#content', '#main',
    'div.TRS_Editor', 'div.Custom_UnionStyle',
]

# 正文内部需要移除的噪声（分享按钮、相关文章等）
CONTENT_NOISE_PATTERNS = [
    'share', 'social', 'print', 'toolbar', 'related',
    'recommend', 'pagination', 'page-nav', 'copyright',
    '声明', '转载', '附件', '上一篇', '下一篇', '关闭',
]

# 正文内部噪声标签
CONTENT_NOISE_TAGS = {'script', 'style', 'iframe', 'form', 'input',
                       'button', 'noscript'}


def extract_article_content(html, url, content_selector=None):
    """从通知详情页提取正文。

    策略（按优先级）：
    1. 使用指定的 content_selector（如果有）
    2. 尝试常见文章容器选择器
    3. 文本密度分析 (find_main_content)
    4. 回退到 <body> 全文

    Args:
        html: 页面 HTML
        url: 页面 URL
        content_selector: 可选，数据库配置的正文选择器

    Returns:
        dict: {
            'content_html': str,       # 清洗后的正文 HTML
            'content_text': str,       # 纯文本
            'content_length': int,     # 文本长度
            'confidence': float,       # 置信度
            'method': str,             # 使用的提取方法
            'selector_used': str,      # 实际使用的选择器
        }
    """
    soup = BeautifulSoup(html, 'lxml')

    content_element = None
    method = 'fallback'
    selector_used = 'body'

    # ---- 策略 1: 使用指定选择器 ----
    if content_selector:
        for sel in content_selector.split(','):
            sel = sel.strip()
            try:
                content_element = soup.select_one(sel)
                if content_element:
                    method = 'configured_selector'
                    selector_used = sel
                    break
            except Exception:
                continue

    # ---- 策略 2: 尝试常见容器 ----
    if not content_element:
        for sel in CONTENT_SELECTORS:
            try:
                content_element = soup.select_one(sel)
                if content_element:
                    text = content_element.get_text(strip=True)
                    if len(text) >= 100:
                        method = 'semantic_match'
                        selector_used = sel
                        break
                    else:
                        content_element = None  # 文本太短继续尝试
            except Exception:
                continue

    # ---- 策略 3: 文本密度分析 ----
    if not content_element:
        density_result = find_main_content(soup)
        if density_result and density_result['text_length'] >= 200:
            try:
                content_element = soup.select_one(density_result['selector'])
                if content_element:
                    method = 'text_density'
                    selector_used = density_result['selector']
            except Exception:
                pass

    # ---- 策略 4: 回退到 body ----
    if not content_element:
        content_element = soup.find('body')
        method = 'body_fallback'
        selector_used = 'body'

    if not content_element:
        return {
            'content_html': '',
            'content_text': '(无法提取正文)',
            'content_length': 0,
            'confidence': 0,
            'method': 'failed',
            'selector_used': '',
        }

    # ---- 清理正文 ----
    content_html = _clean_content_html(content_element, url)
    content_text = _extract_clean_text(content_element)

    # 置信度评估
    confidence = _assess_confidence(content_text, method)

    return {
        'content_html': content_html,
        'content_text': content_text,
        'content_length': len(content_text),
        'confidence': round(confidence, 2),
        'method': method,
        'selector_used': selector_used,
    }


def _clean_content_html(element, base_url):
    """清理正文 HTML。

    - 移除噪声标签（分享、打印、相关文章等）
    - 重写相对链接为绝对 URL
    - 保留安全的格式标签
    """
    # 复制元素以免修改原始 DOM
    cleaned = BeautifulSoup(str(element), 'lxml')

    # 移除噪声标签
    for tag_name in CONTENT_NOISE_TAGS:
        for tag in cleaned.find_all(tag_name):
            tag.decompose()

    # 移除噪声区域
    for pattern in CONTENT_NOISE_PATTERNS:
        for tag in cleaned.find_all(
            class_=lambda v: v and pattern.lower() in ' '.join(v).lower()
        ):
            tag.decompose()
        for tag in cleaned.find_all(
            id=lambda v: v and pattern.lower() in v.lower()
        ):
            tag.decompose()

    # 移除空标签（但保留 <br>, <img>, <hr>）
    for tag in cleaned.find_all(True):
        if tag.name in ('br', 'img', 'hr', 'input'):
            continue
        if not tag.get_text(strip=True) and not tag.find('img'):
            tag.decompose()

    # 重写相对链接
    for a_tag in cleaned.find_all('a', href=True):
        href = a_tag['href']
        if href and not href.startswith(('http://', 'https://', '//', '#', 'javascript:', 'mailto:')):
            a_tag['href'] = urljoin(base_url, href)

    for img_tag in cleaned.find_all('img', src=True):
        src = img_tag['src']
        if src and not src.startswith(('http://', 'https://', '//', 'data:')):
            img_tag['src'] = urljoin(base_url, src)

    return str(cleaned)


def _extract_clean_text(element):
    """从元素提取纯文本。"""
    if element is None:
        return ''

    # 移除 script/style
    for tag_name in ('script', 'style', 'nav', 'footer', 'header'):
        for tag in element.find_all(tag_name):
            tag.decompose()

    text = element.get_text(separator='\n', strip=True)

    # 清理多余空白
    import re
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    text = re.sub(r' {3,}', '  ', text)

    return text.strip()


def _assess_confidence(text, method):
    """评估提取正文的置信度。"""
    if not text:
        return 0

    length = len(text)

    # 太短
    if length < 50:
        return 0.1
    if length < 100:
        return 0.3

    # 方法加分
    method_bonus = {
        'configured_selector': 0.2,
        'semantic_match': 0.15,
        'text_density': 0.1,
        'body_fallback': 0,
        'failed': 0,
    }.get(method, 0)

    # 长度加分
    if length > 2000:
        length_bonus = 0.1
    elif length > 500:
        length_bonus = 0.05
    else:
        length_bonus = 0

    # 文本质量检查
    quality_penalty = 0
    # 太多导航类文字
    nav_indicators = sum(text.count(kw) for kw in
                          ['首页', '末页', '登录', '注册', '版权所有'])
    if nav_indicators > 3:
        quality_penalty -= 0.2
    # 大量空格
    if text.count(' ') > len(text) * 0.2:
        quality_penalty -= 0.1

    confidence = 0.5 + method_bonus + length_bonus + quality_penalty
    return max(0, min(1, confidence))
