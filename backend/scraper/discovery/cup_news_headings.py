"""Column boundaries observed on CUP's current news homepage."""
from urllib.parse import urlsplit
import re

from backend.services.source_inventory import resolve_page_link


def cup_news_heading(item, url):
    page = urlsplit(url)
    if page.hostname != 'www.cup.edu.cn' or page.path not in ('/news/', '/news/index.htm'):
        return None
    from .structure import clean, locator
    scope = item.find_parent(class_='Wrapmode01')
    if scope is not None:
        # The top title belongs to both the carousel and its adjacent news list.
        labels = scope.select('.listTitle01 > h2 > a[href]')
        links = labels
    else:
        scope = next((p for p in item.parents if any(re.fullmatch(r'articleList\d+', c)
                      for c in p.get('class', []))), None)
        if scope is None:
            return None
        labels = scope.select(':scope > .articleTitle01 > .title')
        links = scope.select(':scope > .articleTitle01 > .more > a[href]')
    if len(labels) != 1:
        return {'name': '', 'heading_locator': '', 'column_url': '',
                'column_link_locator': '', 'heading_ambiguous': True}
    name = clean(labels[0].get_text(' ', strip=True))
    targets = {resolve_page_link(url, a.get('href')) for a in links}
    targets.discard('')
    if not 2 <= len(name) <= 40 or len(targets) != 1:
        return {'name': '', 'heading_locator': '', 'column_url': '',
                'column_link_locator': '', 'heading_ambiguous': True}
    target = targets.pop()
    link = next(a for a in links if resolve_page_link(url, a.get('href')) == target)
    return {'name': name, 'heading_locator': locator(labels[0]), 'column_url': target,
            'column_link_locator': locator(link), 'heading_method': 'official_cup_news_module'}
