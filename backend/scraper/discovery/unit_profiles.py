"""Recognize reviewed unit-introduction templates separately from unit websites."""
import re
from urllib.parse import urlsplit

from backend.services.source_inventory import canonical_url, resolve_page_link


def nju_business_profile(soup, page_url):
    """Require the article, breadcrumb, section and selected directory to agree.

    The page title alone cannot prove this role. This template belongs to the
    business school's shared site, including its two college introduction pages.
    """
    if urlsplit(page_url).hostname != 'nubs.nju.edu.cn':
        return None
    from .structure import clean, locator
    headings = soup.select('.col_title > h2')
    articles = soup.select('.wp_articlecontent')
    sections = soup.select('.col_name .Column_Anchor')
    if (len(headings) != 1 or len(articles) != 1 or len(sections) != 1 or
            clean(sections[0].get_text()) != '学院一览'):
        return None
    name = clean(headings[0].get_text(' ', strip=True))
    crumbs = [a for a in soup.select('.col_path > a') if clean(a.get_text()) == '学院一览'
              and urlsplit(resolve_page_link(page_url, a.get('href'))).path == '/8877/list.htm']
    selected = [a for a in soup.select('.col_list .wp_listcolumn a.selected')
                if clean(a.get_text(' ', strip=True)) == name
                and resolve_page_link(page_url, a.get('href')) == canonical_url(page_url)]
    if len(crumbs) != 1 or len(selected) != 1:
        return None
    branches = [li for li in soup.select('.col_list .wp_listcolumn > li')
                if any(p is li for p in selected[0].parents)]
    if len(branches) != 1:
        return None
    branch = branches[0]
    primary = branch.find('a', recursive=False)
    if primary is None or not clean(primary.get_text(' ', strip=True)).endswith('学院'):
        return None
    text = articles[0].get_text(' ', strip=True)
    return {'branch': branch, 'evidence': {
        'template': 'nju_business_introduction', 'unit_name': name,
        'heading_locator': locator(headings[0]), 'body_locator': locator(articles[0]),
        'section_locator': locator(sections[0]), 'breadcrumb_locator': locator(crumbs[0]),
        'selected_locator': locator(selected[0]), 'branch_locator': locator(branch),
        'content_pending': bool(re.fullmatch(r'内容正在更新[\s.…。]*', text)),
    }}
