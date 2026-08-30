"""通用翻页检测器

整合 engine.py 中的翻页检测逻辑和 dom_analyzer 的 find_pagination_links，
提供统一的翻页格式检测和管理。
"""

import logging
import re
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from curl_cffi import requests

from backend.scraper.detectors.dom_analyzer import find_pagination_links
from backend.scraper.engine import HEADERS, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# 已知翻页格式（从 engine.py 提取）
PAGINATION_STYLES = {
    'default': {
        'name': 'ZCMS shtml 翻页',
        'template': '{base}/index_{page}.shtml',
        'page_1_is_base': True,
    },
    'catalog': {
        'name': 'ZCMS catalog 翻页',
        'template': '{base}/index_{page}.shtml',
        'page_1_is_base': False,
    },
    'index_htm': {
        'name': 'index_N.htm 翻页',
        'template': '{base}/index_{page}.htm',
        'page_1_is_base': True,
    },
    'index_html': {
        'name': 'index_N.html 翻页（上交 ETUI 等自建站）',
        'template': '{base}/index_{page}.html',
        'page_1_is_base': True,
    },
    'index_htm_offset': {
        'name': '零偏移 index 翻页',
        'template': '{base}/index{offset}.htm',  # page N → index{N-1}.htm
        'page_1_is_base': True,
    },
    'query_page': {
        'name': '?page=N 翻页',
        'template': '{base}?page={page}',
        'page_1_is_base': True,
    },
    'query_page_index': {
        'name': 'index.htm?page=N 翻页',
        'template': '{base}?page={page}',
        'page_1_is_base': True,
    },
    'page_path': {
        'name': '/page/N/ 路径翻页',
        'template': '{base}/page/{page}/',
        'page_1_is_base': True,
    },
    'default_htm': {
        'name': '/N.htm 翻页',
        'template': '{base}/{page}.htm',
        'page_1_is_base': True,
    },
    'list_htm': {
        'name': '苏迪 list{N}.htm 翻页',
        'template': '{base}/list{page}.htm',
        'page_1_is_base': True,
    },
    'query_page_zero': {
        'name': 'Drupal ?page=N 零起翻页',
        'template': '{base}?page={offset}',  # page N → ?page={N-1}
        'page_1_is_base': True,
    },
}


def detect_pagination(html, base_url):
    """综合检测页面翻页格式。

    1. HTML 预判（从翻页链接提取模式）
    2. HTTP 探测（尝试已知格式）

    Args:
        html: 列表页第1页 HTML
        base_url: 列表页 URL

    Returns:
        dict: {style, page_param, max_page, confidence, detection_method}
        或 None
    """
    from bs4 import BeautifulSoup

    # 策略 1: DOM 分析
    soup = BeautifulSoup(html, 'lxml')
    result = find_pagination_links(soup, base_url)
    if result and result['confidence'] >= 0.6:
        result['detection_method'] = 'dom_analysis'
        return result

    # 策略 2: HTTP 探测已知格式
    http_result = _probe_pagination_http(base_url)
    if http_result:
        http_result['detection_method'] = 'http_probe'
        return http_result

    # 策略 3: 返回默认格式（最常见）
    return {
        'style': 'default',
        'page_param': None,
        'max_page': 50,
        'confidence': 0.3,
        'detection_method': 'default_fallback',
        'sample_urls': [],
    }


def _probe_pagination_http(base_url):
    """通过 HTTP 探测翻页格式。"""
    probe_formats = [
        ('index_shtml', lambda u: _get_page_url_static(u, 'default', 2)),
        ('index_htm', lambda u: _get_page_url_static(u, 'index_htm', 2)),
        ('index_html', lambda u: _get_page_url_static(u, 'index_html', 2)),
        ('index_htm_offset', lambda u: _get_page_url_static(u, 'index_htm_offset', 2)),
        ('query_page_index', lambda u: _get_page_url_static(u, 'query_page_index', 2)),
        ('query_page', lambda u: _get_page_url_static(u, 'query_page', 2)),
        ('page_path', lambda u: _get_page_url_static(u, 'page_path', 2)),
        ('default_htm', lambda u: _get_page_url_static(u, 'default_htm', 2)),
        ('list_htm', lambda u: _get_page_url_static(u, 'list_htm', 2)),
        ('query_page_zero', lambda u: _get_page_url_static(u, 'query_page_zero', 2)),
    ]

    for style, url_builder in probe_formats:
        try:
            page2_url = url_builder(base_url)
            if not page2_url:
                continue

            # HEAD 请求优先（快速）
            try:
                resp = requests.head(page2_url, headers=HEADERS, timeout=5, allow_redirects=False, impersonate='chrome')
                if resp.status_code == 200:
                    return {'style': style, 'page_param': None, 'max_page': 50, 'confidence': 0.5}
            except Exception:
                pass

            # GET 请求验证
            try:
                resp = requests.get(page2_url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                                    stream=True, impersonate='chrome')
                content_preview = b''
                for chunk in resp.iter_content(1024):
                    content_preview += chunk
                    if len(content_preview) >= 1024:
                        break
                resp.close()

                content_str = content_preview.decode('utf-8', errors='ignore')
                # 检查是否像列表页（不是错误页）
                if resp.status_code == 200 and len(content_str) > 500:
                    # 排除错误页特征
                    if not any(kw in content_str[:500].lower() for kw in
                               ['not found', '404', '页面不存在', '找不到']):
                        return {
                            'style': style,
                            'page_param': None,
                            'max_page': 50,
                            'confidence': 0.5,
                        }
            except Exception:
                continue
        except Exception:
            continue

    return None


def _get_page_url_static(base_url, style, page_num):
    """根据格式生成翻页 URL（静态版本，不依赖 engine.py）。"""
    parsed = urlparse(base_url)

    if style == 'catalog' or (style == 'default' and '/zcms/catalog/' in base_url):
        # ZCMS catalog: 所有页面都是 index_N.shtml
        return _build_index_n(base_url, 'shtml', page_num)

    if style == 'default':
        if '/zcms/catalog/' in base_url:
            return _build_index_n(base_url, 'shtml', page_num)
        return _build_index_n(base_url, 'shtml', page_num)

    if style == 'index_htm':
        return _build_index_n(base_url, 'htm', page_num)

    if style == 'index_html':
        return _build_index_n(base_url, 'html', page_num)

    if style == 'index_htm_offset':
        # page 2 → index1.htm
        offset = page_num - 1
        if '/index' in parsed.path:
            path = re.sub(r'/index\d*\.htm$', f'/index{offset}.htm', parsed.path)
        elif parsed.path.endswith('/index.htm'):
            path = parsed.path[:-len('index.htm')] + f'index{offset}.htm'
        else:
            return None
        return urlunparse(parsed._replace(path=path))

    if style == 'query_page_index':
        query = parse_qs(parsed.query)
        query['page'] = [str(page_num)]
        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    if style == 'query_page':
        query = parse_qs(parsed.query)
        query['page'] = [str(page_num)]
        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    if style == 'query_page_zero':
        # Drupal：第 1 页无参数，第 N 页 ?page={N-1}
        query = parse_qs(parsed.query)
        query['page'] = [str(page_num - 1)]
        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    if style == 'list_htm':
        # 苏迪：list.htm 为第 1 页，第 N 页 list{N}.htm
        path = re.sub(r'/list\d*\.htm$', f'/list{page_num}.htm', parsed.path)
        if path == parsed.path:
            path = parsed.path.rstrip('/') + f'/list{page_num}.htm'
        return urlunparse(parsed._replace(path=path))

    if style == 'page_path':
        parts = parsed.path.rstrip('/').split('/')
        if parts and parts[-1].isdigit():
            parts[-1] = str(page_num)
        else:
            parts.append(str(page_num))
        path = '/'.join(parts)
        return urlunparse(parsed._replace(path=path))

    if style == 'default_htm':
        dir_path = parsed.path.rsplit('/', 1)[0] if '/' in parsed.path else ''
        return urlunparse(parsed._replace(path=f'{dir_path}/{page_num}.htm'))

    return None


def _build_index_n(base_url, ext, page_num):
    """构建 index_N.ext 格式的 URL。"""
    parsed = urlparse(base_url)
    path = parsed.path

    # 已经是 index_N.ext 格式
    if re.search(r'/index_\d+\.\w+$', path):
        path = re.sub(r'/index_\d+', f'/index_{page_num}', path)
    # 带文件名
    elif path.endswith(('.shtml', '.htm', '.html', '.jsp', '.asp', '.aspx', '.php')):
        base = path.rsplit('/', 1)[0] if '/' in path else ''
        path = f'{base}/index_{page_num}.{path.rsplit(".", 1)[1]}'
    # 目录
    else:
        path = path.rstrip('/') + f'/index_{page_num}.{ext}'

    return urlunparse(parsed._replace(path=path))


def get_page_url(base_url, style, page_num):
    """根据检测到的格式生成翻页 URL。

    统一接口，替代 engine.py 中的 _get_page_url()。

    Args:
        base_url: 列表页第1页 URL
        style: 翻页格式名
        page_num: 页码

    Returns:
        str: 翻页 URL
    """
    if page_num == 1:
        return base_url

    return _get_page_url_static(base_url, style, page_num)
