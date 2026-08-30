"""
HTML 内容安全清洗器
===================
在爬取到的 HTML 存入数据库之前，移除潜在的 XSS 攻击向量。
基于 BeautifulSoup（项目已有依赖），无需额外安装。
"""

import re
from bs4 import BeautifulSoup, Comment

# ---- 要完全移除的标签 ----
REMOVE_TAGS = {
    'script', 'style', 'iframe', 'object', 'embed',
    'form', 'input', 'textarea', 'button', 'select', 'option',
    'link', 'meta', 'base', 'applet', 'noscript',
}

# ---- 危险的事件属性前缀 ----
DANGEROUS_ATTR_PREFIXES = ('on',)  # onclick, onerror, onload, onmouseover, ...

# ---- 危险协议 ----
DANGEROUS_PROTOCOLS = ('javascript:', 'vbscript:', 'data:text/html')


def sanitize_html(html_str: str) -> str:
    """清洗 HTML 字符串，移除危险标签、属性和协议。

    返回安全的 HTML 字符串，保留正常的排版和链接结构。

    Args:
        html_str: 原始 HTML 字符串

    Returns:
        清洗后的安全 HTML 字符串
    """
    if not html_str or not html_str.strip():
        return html_str

    soup = BeautifulSoup(html_str, 'lxml')

    # 1. 移除 HTML 注释（可能隐藏恶意代码）
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    # 2. 移除危险标签
    for tag_name in REMOVE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # 2.5 移除「当前位置」面包屑（正文选择器常把页头导航误包进来）
    for text in soup.find_all(string=lambda t: t and t.strip().startswith(('当前位置：', '当前位置:'))):
        block = text.find_parent(['div', 'p', 'nav'])
        if block:
            block.decompose()

    # 3. 清洗所有元素的危险属性
    for element in soup.find_all(True):  # True = 所有标签
        attrs_to_remove = []

        for attr_name, attr_value in list(element.attrs.items()):
            # 移除事件处理器属性（onclick, onerror, onload 等）
            if any(attr_name.lower().startswith(prefix)
                   for prefix in DANGEROUS_ATTR_PREFIXES):
                attrs_to_remove.append(attr_name)
                continue

            # 清洗 href/src 中的危险协议
            if attr_name in ('href', 'src'):
                if isinstance(attr_value, str):
                    value_lower = attr_value.strip().lower()
                    if any(value_lower.startswith(proto)
                           for proto in DANGEROUS_PROTOCOLS):
                        element[attr_name] = '#blocked'

            # 清洗 style 属性中的危险表达式
            if attr_name == 'style':
                if isinstance(attr_value, str):
                    cleaned = _sanitize_style_value(attr_value)
                    if cleaned != attr_value:
                        element['style'] = cleaned
                    if not cleaned.strip():
                        attrs_to_remove.append('style')

        # 执行属性移除
        for attr in attrs_to_remove:
            del element[attr]

    return str(soup)


def _sanitize_style_value(style_str: str) -> str:
    """清洗内联 style 属性中的危险 CSS 表达式。

    移除包含 expression()、url()、@import、behavior 等危险 CSS 的值。
    """
    if not style_str:
        return style_str

    # 移除包含 expression() 的声明（IE CSS 表达式 — XSS 向量）
    if 'expression(' in style_str.lower():
        # 过滤整个 style 中所有含 expression 的属性
        parts = []
        for decl in style_str.split(';'):
            if 'expression(' not in decl.lower():
                parts.append(decl)
        style_str = ';'.join(parts)

    # 如果 style 中包含 url()（可能用于加载外部资源），谨慎处理
    # 只保留安全的 url() 用法（如 data: URIs 用于图标）
    if 'url(' in style_str.lower():
        # 简单策略：如果 url() 不是 data:，则清空整个 style
        url_pattern = re.compile(r'url\(\s*["\']?(?!data:)([^)"\']+)', re.IGNORECASE)
        if url_pattern.search(style_str):
            # 有外部 URL 引用，移除整个 style
            return ''

    # 主题兼容：移除背景/文字色/字体族声明。Word 导出的正文常带
    # background:rgb(255,255,255)、color:rgb(17,17,17)，在深色主题下
    # 会渲染成白底黑字条带；保留排版类属性（对齐/缩进/行高/字号等）
    drop_props = ('color', 'font-family')
    parts = []
    for decl in style_str.split(';'):
        prop = decl.split(':', 1)[0].strip().lower()
        if prop in drop_props or prop.startswith('background'):
            continue
        parts.append(decl)
    return ';'.join(parts)
