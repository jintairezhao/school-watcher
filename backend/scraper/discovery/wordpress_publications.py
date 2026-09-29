"""Use observed WordPress post/taxonomy markup, never guess a parent URL."""
import re
from urllib.parse import urlsplit

from backend.services.source_inventory import canonical_url, resolve_page_link

POST_LIST = {'list_selector': 'ul.article-list > li.type-post', 'title_selector': 'span.post a',
             'link_selector': 'span.post a', 'date_selector': 'span.date',
             'content_selector': 'article.type-post', 'method': 'profile_match',
             'confidence': 0.99, 'minimum_items': 1}


def page_kind(soup):
    classes = soup.body.get('class', []) if soup.body else []
    if 'single-post' in classes and soup.select_one('article.type-post[id^="post-"]'):
        return 'article'
    if 'search' in classes:
        return 'search'
    return ''


def article_positions(soup, url):
    from .structure import locator
    result = set()
    for post in soup.select('.type-post'):
        identifiers = [post.get('id', ''), *post.get('class', [])]
        ids = {value[5:] for value in identifiers if re.fullmatch(r'post-\d+', value)}
        for link in post.select('span.post a[href], .entry-title a[href], h2 a[href]'):
            target = resolve_page_link(url, link['href'])
            if target and (any(re.search(r'/' + ident + r'\.html?(?:$|[?#])', target) for ident in ids)
                           or link.find_parent(class_='entry-title')):
                result.add(locator(link))
    if page_kind(soup) == 'article':
        for link in soup.select('h1 a[href]'):
            if resolve_page_link(url, link['href']) == canonical_url(url):
                result.add(locator(link))
    return result


def column_links(soup, url):
    from .structure import clean, locator
    result = []
    if page_kind(soup) == 'article':
        links = soup.select('.meta-categories a[rel~="tag"], a[rel~="category"]')
        if not links:
            links = soup.select('.breadcrumbs a.taxonomy.category[href]')[-1:]
    else:
        links = soup.select('.cat-tags a[href], .meta-categories a[rel~="tag"], '
                            '.breadcrumbs a.taxonomy.category[href], a[rel~="category"]')
    for link in links:
        target = resolve_page_link(url, link.get('href', ''))
        name = clean(link.get_text(' ', strip=True))
        if (target and urlsplit(target).netloc == urlsplit(url).netloc
                and '/category/' in urlsplit(target).path and 1 <= len(name) <= 40):
            result.append({'url': target, 'name': name, 'locator': locator(link)})
    return result


def archive_heading(item, url):
    from .structure import clean, locator
    soup = item
    while soup.parent is not None:
        soup = soup.parent
    if not soup.body or 'category' not in soup.body.get('class', []):
        return None
    if not item.find_parent('main') or not item.find_parent('ul', class_='article-list'):
        return None
    crumbs = soup.select('.breadcrumbs')
    if len(crumbs) != 1:
        return None
    labels = list(crumbs[0].stripped_strings)
    name = clean(labels[-1]) if labels else ''
    headings = [h for h in soup.select('h1') if clean(h.get_text(' ', strip=True)) == name]
    if not 1 <= len(name) <= 40 or len(headings) != 1:
        return None
    return {'name': name, 'heading_locator': locator(headings[0]), 'column_url': canonical_url(url),
            'column_link_locator': locator(crumbs[0]), 'heading_method': 'wordpress_archive_breadcrumb'}
