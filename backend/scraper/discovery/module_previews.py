"""Observe bounded homepage modules without equating previews to full columns."""
from backend.services.source_inventory import canonical_url, resolve_page_link


def visible(node):
    import re
    return not any(parent.has_attr('hidden') or parent.has_attr('inert') or parent.get('aria-hidden')=='true'
        or any(mark in re.sub(r'\s+','',parent.get('style','')).lower() for mark in ('display:none','visibility:hidden'))
        for parent in [node,*node.parents])

GQT_CONFIG = dict(list_selector='div.c-gqt-module__half-width > div.c-gqt-module__detail > ul > li.c-gqt-module__half-li',
    title_selector='a[title],a', link_selector='a[href]', date_selector='.c-gqt-module__detail-content-date',
    content_selector='', minimum_items=1, required_heading_method='reviewed_gqt_module', require_named_heading=True,
    method='reviewed_module', confidence=.95)
CAIWU_CONFIG = dict(list_selector='div.c-caiwu-module-new__content-box-middle > '
    ':is(div.c-caiwu-module-new__content-box-middle-top,div.c-caiwu-module-new__content-box-middle-down) > '
    'div.c-caiwu-module-new__main-content',
    group_container_selector='div.c-caiwu-module-new__content-box-middle',
    title_selector='.c-caiwu-module-new__main-content-words a',
    link_selector='.c-caiwu-module-new__main-content-words a', date_selector='.c-caiwu-module-new__main-content-date a',
    content_selector='', required_heading_method='bounded_homepage_module_preview', require_module_preview=True,
    method='reviewed_module', confidence=.95)


def _simple_news_heading(item, url, *, archive_only=False):
    """Bind a literal CMS news window's own title, list and archive button."""
    import re
    from urllib.parse import urlsplit
    from .structure import ARTICLE, MORE, SKIP, clean, locator
    module = item.find_parent(attrs={'frag': re.compile(r'^窗口\d*$')})
    if module is None or module.get('portletmode') != 'simpleNews':
        return None
    unknown = dict(name='', heading_locator='', column_url='', column_link_locator='',
                   heading_method='bounded_simple_news_window', heading_ambiguous=True,
                   module_preview=False, full_column_equivalent=False)
    heads = module.select(':scope > .tt')
    contents = module.select(':scope > .con')
    # Other simpleNews templates already have their own generic heading proof.
    # The mode alone must not shadow those unrelated markup families.
    if not heads or not contents:
        return None
    if (len(heads) != 1 or len(contents) != 1 or not visible(module)
            or any(child.name not in ('script', 'style') and child not in heads + contents
                   for child in module.find_all(recursive=False))
            or module.select('[frag^="窗口"]')):
        return unknown
    titles = heads[0].select('[frag="标题"]')
    actions = heads[0].select('[frag="按钮"][type="更多"]')
    if len(titles) != 1 or len(actions) != 1:
        return unknown
    links = actions[0].select('a[href]')
    name = clean(titles[0].get_text(' ', strip=True))
    if (len(links) != 1 or not 2 <= len(name) <= 40 or name in MORE | SKIP
            or titles[0].select('a,button,[role="tab"],[onclick]')
            or not all(visible(node) for node in (titles[0], actions[0], links[0]))):
        return unknown
    target = resolve_page_link(url, links[0]['href'])
    if (not target or target == canonical_url(url) or ARTICLE.search(target)
            or urlsplit(target).netloc != urlsplit(url).netloc):
        return unknown
    heading = dict(name=name, heading_locator=locator(titles[0]), column_url=target,
                   column_link_locator=locator(links[0]), heading_method='bounded_simple_news_window')
    if archive_only:
        # A window with no articles still proves its own explicit entrance.
        # It does not prove a nonempty publication list or an organization's owner.
        return heading if links[0] is item else unknown
    lists = contents[0].select('ul.news_list')
    if len(lists) != 1 or not any(parent is lists[0] for parent in [item, *item.parents]):
        return unknown
    return dict(heading, module_preview=True, module_container_locator=locator(lists[0]), full_column_equivalent=False)


def module_heading(item, url):
    from .structure import ARTICLE, MORE, SKIP, clean, locator
    from .publication_lists import is_more
    simple_news = _simple_news_heading(item, url)
    if simple_news is not None:
        return simple_news
    module = item.find_parent(class_='c-gqt-module__half-width')
    if module is not None:
        unknown = dict(name='', heading_locator='', column_url='', column_link_locator='',
                       heading_method='reviewed_gqt_module', heading_ambiguous=True)
        heads = module.select(':scope > .c-gqt-module__head')
        details = module.select(':scope > .c-gqt-module__detail')
        titles = module.select(':scope > .c-gqt-module__head > .c-gqt-module__head-title-container > '
                               ':is(.c-gqt-module__head-title,.c-gqt-module__head-title-color-green)')
        links = module.select(':scope > .c-gqt-module__head > .c-gqt-module__head-more > a[href]')
        if len(heads)!=1 or len(details)!=1 or len(titles)!=1 or len(links)!=1 or not visible(module):
            return unknown
        lists = details[0].find_all('ul', recursive=False)
        if (len(lists)!=1 or not any(parent is lists[0] for parent in item.parents)
                or any(child.name not in ('script','style') and child is not lists[0]
                       for child in details[0].find_all(recursive=False))):
            return unknown
        name = clean(titles[0].get_text(' ', strip=True))
        target = resolve_page_link(url, links[0]['href'])
        if (not visible(titles[0]) or not visible(links[0])
                or not 2<=len(name)<=40 or name in MORE|SKIP or not target or ARTICLE.search(target)
                or not is_more(clean(links[0].get_text(' ',strip=True)))
                or titles[0].select('a[href],button,[onclick],[role="tab"]')):
            return unknown
        return dict(name=name, heading_locator=locator(titles[0]), column_url=target,
                    column_link_locator=locator(links[0]), heading_method='reviewed_gqt_module')

    module = item.find_parent(class_='c-caiwu-module-new__content-box-middle')
    if module is None:
        return None
    unknown = dict(name='', heading_locator='', column_url='', column_link_locator='',
                   heading_method='bounded_homepage_module_preview', heading_ambiguous=True)
    parts = ('c-caiwu-module-new__content-box-middle-top','c-caiwu-module-new__content-box-middle-down',
             'c-caiwu-module-new__content-box-middle-more')
    if (not visible(module) or any(len(module.select(':scope > .'+part))!=1 for part in parts)
            or any(child.name not in ('script','style') and not set(parts).intersection(child.get('class',[]))
                   for child in module.find_all(recursive=False))):
        return unknown
    top,down,more = [module.select_one(':scope > .'+part) for part in parts]
    leads = top.select(':scope > .c-caiwu-module-new__main-content')
    rows = down.select(':scope > .c-caiwu-module-new__main-content')
    links = more.select('a[href]')
    if len(leads)!=1 or not rows or len(links)!=1 or not is_more(clean(links[0].get_text(' ',strip=True))):
        return unknown
    if any(child.name not in ('script','style') and not any(child is row for row in rows)
           for child in down.find_all(recursive=False)):
        return unknown
    lead_links = leads[0].select('.c-caiwu-module-new__main-content-words a[href]')
    if len(lead_links)!=1:
        return unknown
    lead_url = resolve_page_link(url, lead_links[0]['href'])
    # The separate lead summary may repeat that same article, but an unrelated
    # link would make the module composition ambiguous.
    for link in top.select('a[href]'):
        if not resolve_page_link(url,link['href']) or resolve_page_link(url,link['href'])!=lead_url:
            return unknown
    target = resolve_page_link(url, links[0]['href'])
    if not target or target==canonical_url(url) or ARTICLE.search(target):
        return unknown
    return dict(unknown, column_url=target, column_link_locator=locator(links[0]), module_preview=True,
                module_container_locator=locator(module),
                full_column_equivalent=False)
