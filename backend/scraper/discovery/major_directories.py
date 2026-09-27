"""Reviewed professional directories, distinct from dated notification lists.

An entry describes a programme on this directory page. It does not establish an
independent publishing unit, nor connect it to a department by name similarity.
"""
from urllib.parse import urlsplit

from backend.services.source_inventory import resolve_page_link


def major_scopes(soup, url):
    """Return only the list regions whose professional context was reviewed."""
    parsed = urlsplit(url)
    if parsed.hostname != 'www.cupk.edu.cn':
        from .programme_catalogs import catalogue_scopes
        return catalogue_scopes(soup, url)
    if parsed.path == '/sgxy/bksjy/bkzysz/':
        return soup.select('ul.middleArticle__articleList')
    if parsed.path in ('/jdxy/bkjy/zysz/jxsjzzjqzdh/',
                       '/jdxy/bkjy/zysz/gczbykzgc/', '/jdxy/zdh/'):
        # The selected sidebar supplies the context, not words in the article.
        selected = soup.select('.middleLeft__left--nav > .middleLeft__left--navClick > a')
        if len(selected) == 1 and selected[0].get_text(strip=True) == '专业介绍':
            return soup.select('ul.middleTop__nav, .middleArticle__nav > ul')
    return []


def major_relationships(soup, url, owner_key):
    from .structure import clean, locator, node_key
    nodes, links, locations = [], [], set()
    for scope in major_scopes(soup, url):
        for row in scope.find_all('li', recursive=False):
            anchors = row.find_all('a', href=True)
            if len(anchors) != 1:
                continue
            anchor = anchors[0]
            name = clean(anchor.get_text(' ', strip=True))
            target = resolve_page_link(url, anchor.get('href'))
            if not name or len(name) > 50 or not target:
                continue
            # The dated petroleum directory uses article URLs for its profiles.
            # Follow those profiles while retaining the original displayed name.
            position = locator(anchor)
            locations.add(position)
            nodes.append({'key': node_key('major', name, target), 'name': name,
                          'kind': 'major', 'url': target, 'parent': owner_key,
                          'locator': position, 'relation': 'major_directory_entry'})
            links.append({'label': name, 'url': target, 'kind': 'major',
                          'path': ['专业介绍'], 'locator': position,
                          'decision': 'follow' if urlsplit(target).hostname == urlsplit(url).hostname
                                      else 'external_review'})
    from .programme_catalogs import catalogue_relationships
    extra_nodes, extra_links, extra_locations, notes = catalogue_relationships(soup, url, owner_key)
    return nodes + extra_nodes, links + extra_links, locations | extra_locations, notes
