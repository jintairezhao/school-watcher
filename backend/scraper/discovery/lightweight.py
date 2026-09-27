"""Discover visible official columns in one homepage request, no site-wide crawl."""
import re

from bs4 import BeautifulSoup

from backend.scraper.http_client import same_school_url
from backend.services.source_inventory import resolve_page_link

CHANNEL_WORDS = ('通知', '公告', '新闻', '资讯', '教务', '招生', '就业', '学术', '科研',
                 '科学研究', '学生工作', '图书馆', '研究生院', '本科生院', '信息公开')


def discover_columns(school_url, html=None):
    if html is None:
        from backend.scraper.engine import _fetch_html
        html = _fetch_html(school_url, raise_fetch_errors=True, purpose='directory')
    if not html:
        return []
    school_url = getattr(html, 'final_url', school_url)
    soup = BeautifulSoup(html, 'lxml')
    result, seen = [], set()
    for anchor in soup.select('a[href]'):
        name = re.sub(r'\s+', ' ', anchor.get_text(' ', strip=True))
        if not (2 <= len(name) <= 18) or not any(word in name for word in CHANNEL_WORDS):
            continue
        # Exclude individual notices, account areas and download links.
        href = anchor.get('href', '').strip()
        url = resolve_page_link(school_url, href)
        if (not url or not same_school_url(url, school_url)
                or re.search(r'/(info|article)/|\.(pdf|docx?|xlsx?|zip)$', url, re.I)
                or re.search(r'https?://(portal|auth|sso|id|vpn|webvpn|i)\.', url, re.I)
                or any(word in name for word in ('登录', '系统', '平台', '管理办法', '概况', '简介'))):
            continue
        key = url.rstrip('/')
        if key in seen:
            continue
        group = ''
        # Use only actual ancestor navigation labels, never inferred editorial tags.
        parent_li = anchor.find_parent('li')
        if parent_li:
            outer_li = parent_li.find_parent('li')
            outer_link = outer_li.find('a', recursive=False) if outer_li else None
            if outer_link:
                group = outer_link.get_text(' ', strip=True)[:200]
        result.append(dict(name=name, group_name=group, list_url=url, list_selector='',
                           title_selector='', link_selector='', date_selector='', content_selector=''))
        seen.add(key)
    # Give actual feeds/admissions priority over descriptive research landing pages.
    priority = ('通知', '公告', '新闻', '资讯', '招生', '就业', '教务', '学术')
    result.sort(key=lambda row: next((i for i, word in enumerate(priority) if word in row['name']), len(priority)))
    return result
