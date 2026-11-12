"""Current-page breadcrumb evidence for reviewed listing layouts."""
from backend.services.source_inventory import canonical_url, resolve_page_link


def right_news_breadcrumb_heading(item, url, *, column_url=None):
    """Bind a sole right-hand news list to its adjacent terminal self-link."""
    from .structure import ARTICLE, MORE, SKIP, clean, locator
    listing = (item if item.name == 'ul' and 'list-ul' in item.get('class', [])
               else item.find_parent('ul', class_='list-ul'))
    content = listing.find_parent('div', class_='news-text') if listing is not None else None
    right = content.parent if content is not None else None
    if right is None or 'right' not in right.get('class', []):
        return None
    unknown = dict(name='', heading_locator='', column_url='', column_link_locator='',
                   heading_method='current_page_breadcrumb', heading_ambiguous=True)
    contents = right.select(':scope > .news-text')
    crumbs = right.select(':scope > .local')
    if (len(contents) != 1 or len(crumbs) != 1
            or len(content.select('ul,ol')) != 1
            or any(child.name not in ('script','style','h3')
                   and not {'local','news-text'}.intersection(child.get('class', []))
                   for child in right.find_all(recursive=False))):
        return unknown
    links = crumbs[0].select('a[href]')
    if not links:
        return unknown
    current = links[-1]
    name = clean(current.get_text(' ', strip=True))
    target = resolve_page_link(url, current['href'])
    if (target != canonical_url(column_url or url) or not 2 <= len(name) <= 40
            or name in MORE | SKIP or ARTICLE.search(target)):
        return unknown
    return dict(name=name, heading_locator=locator(current), column_url=target,
                column_link_locator=locator(current), heading_method=(
                    'configured_column_breadcrumb' if column_url else 'current_page_breadcrumb'))


def zcms_right_local_heading(item, url):
    """Name one complete right-hand card list from agreeing local controls."""
    import re
    from .structure import ARTICLE, MORE, SKIP, clean, locator
    families = (('middleArticleNew__art', 'middleArticleNew__art--art'),
                ('middleArticle__art', 'middleArticle__art--art'),
                ('SecondList__list', 'SecondList__list--list'))
    for container_class, row_class in families:
        listing = item.find_parent('div', class_=container_class)
        if listing is not None:
            break
    else:
        return None
    # The same outer CMS class also wraps ordinary UL/LI lists. Leave those
    # to their existing breadcrumb adapter rather than shadowing it.
    if not any(child.name == 'div' and row_class in child.get('class', [])
               for child in listing.find_all(recursive=False)):
        return None
    right = listing.parent
    layout = right.parent if right is not None else None
    if (layout is None or 'c-main__right' not in right.get('class', [])
            or 'c-main--center' not in layout.get('class', [])):
        return None
    unknown = dict(name='', heading_locator='', column_url='', column_link_locator='',
                   heading_method='right_local_selected_self_link', heading_ambiguous=True)
    second = container_class == 'SecondList__list'
    top_class = 'SecondTop-nav__top' if second else 'c-main__top'
    allowed = ('c-main__left', 'c-main__right', top_class)
    if (any(len(layout.select(':scope > .' + part)) != 1 for part in ('c-main__left', 'c-main__right'))
            or len(layout.select(':scope > .' + top_class)) > 1
            or any(child.name not in ('script', 'style') and not set(allowed).intersection(child.get('class', []))
                   for child in layout.find_all(recursive=False))):
        return unknown

    def pagination_only(node):
        controls = r'(?:\d+|首页|尾页|第一页|最后一页|末页|最末页|上一页|下一页|上页|下页|[<>‹›«»…]+)'
        if any(not re.fullmatch(controls, link.get_text(' ', strip=True)) for link in node.select('a[href]')):
            return False
        residual = re.sub(r'共\s*\d+\s*条记录|每页\s*\d+\s*条|第\s*页|' + controls,
                          '', node.get_text(' ', strip=True))
        return not re.sub(r'[\s,，:：/|;；.·()（）-]+', '', residual) and not node.select('img,iframe,object')

    for child in right.find_all(recursive=False):
        if child is listing or child.name in ('script', 'style'):
            continue
        if 'middlePag' not in child.get('class', []) or not pagination_only(child):
            return unknown
    for child in listing.find_all(recursive=False):
        if child.name in ('script', 'style', 'hr') or child.name == 'div' and row_class in child.get('class', []):
            continue
        # Some cards put their empty thumbnail anchor beside the text card.
        # It is the same observed article only when the immediately preceding
        # card links to this exact address; another sibling link is a widget.
        previous = child.find_previous_sibling()
        if (container_class == 'middleArticle__art' and child.name == 'a'
                and 'middleArticle__art--img' in child.get('class', []) and not child.get_text(strip=True)
                and not child.select('a,button,script,iframe,object') and previous is not None
                and row_class in previous.get('class', [])
                and (target := resolve_page_link(url, child.get('href')))
                and target in {resolve_page_link(url, a['href']) for a in previous.select('a[href]')}):
            continue
        if not (second and 'SecondList__list--pagination' in child.get('class', []) and pagination_only(child)):
            return unknown
    left_marker = 'SecondLeft-nav__left--navClick' if second else 'middleLeft__left--navClick'
    top_marker = 'SecondTop-nav__top--navClick' if second else 'middleTop__navClick'

    def visible(link):
        for node in [link, *link.parents]:
            style = re.sub(r'\s+', '', node.get('style', '')).lower()
            if node.has_attr('hidden') or node.get('aria-hidden') == 'true' or node.has_attr('inert') or any(
                    marker in style for marker in ('display:none', 'visibility:hidden')):
                return False
        return True

    left = [a for a in layout.select(':scope > .c-main__left li.' + left_marker + ' > a[href]') if visible(a)]
    top = [a for a in layout.select(':scope > .' + top_class + ' li.' + top_marker + ' > a[href]') if visible(a)]
    # Responsive controls may repeat the same observed label/address. Distinct
    # selected sidebar entries or any conflicting control remain ambiguous.
    if len(left) > 1 or not second and len(top) > 1 or not left and not top:
        return unknown
    controls = left + top
    signatures = {(clean(a.get_text(' ', strip=True)), resolve_page_link(url, a['href'])) for a in controls}
    if len(signatures) != 1:
        return unknown
    name, target = next(iter(signatures))
    if not target or ARTICLE.search(target) or name in MORE | SKIP or not 2 <= len(name) <= 40:
        return unknown
    alias = None
    if target != canonical_url(url):
        from .publication_lists import listing_alias
        alias = listing_alias(listing, controls[0], canonical_url(url), target)
        if not alias:
            return unknown
    return dict(name=name, heading_locator=locator(controls[0]), column_url=target,
                column_link_locator=locator(controls[0]), heading_method='right_local_selected_self_link',
                **({'listing_alias': alias} if alias else {}))


def zcms_local_navigation_heading(item, url):
    """Bind a marked local self-link to a sole adjacent card or article list."""
    import re
    from .structure import ARTICLE, MORE, SKIP, clean, locator
    listing = item.find_parent(id='list_container')
    if listing is not None and 'list_box' in listing.get('class', []):
        content, layout = listing.parent, listing.parent.parent
        if ('c-main__right' not in content.get('class', []) or
                'c-main--center' not in layout.get('class', [])):
            return None
        controls = ':scope > .c-main__left li.middleLeft__left--navClick > a[href]'
        layout_parts = ('c-main__left', 'c-main__right')
        item_tag = 'li'
    else:
        listing = item.find_parent('div', class_='middleArticle__art')
        if listing is None or listing.parent is None or listing.parent.parent is None:
            return None
        content, layout = listing.parent, listing.parent.parent
        if ('c-main--bottom' not in content.get('class', []) or
                'c-main--center' not in layout.get('class', [])):
            return None
        controls = ':scope > .c-main--top li.middleTop__navClick > a[href]'
        layout_parts = ('c-main--top', 'c-main--bottom')
        item_tag = 'ul'
    unknown = dict(name='', heading_locator='', column_url='', column_link_locator='',
                   heading_method='local_selected_self_link', heading_ambiguous=True)
    children = layout.find_all(recursive=False)
    if (any(len(layout.select(':scope > .' + part)) != 1 for part in layout_parts)
            or any(child.name not in ('script', 'style') and not set(layout_parts).intersection(child.get('class', []))
                   for child in children)):
        return unknown
    # A selected navigation entry cannot lend its identity to another widget.
    for child in content.find_all(recursive=False):
        if child is listing or child.name in ('script', 'style'):
            continue
        if 'middlePag' not in child.get('class', []):
            return unknown
        pager_labels = r'(?:\d+|首页|尾页|第一页|最后一页|末页|最末页|上一页|下一页|上页|下页|[<>‹›«»…]+)'
        if any(not re.fullmatch(pager_labels, a.get_text(' ', strip=True)) for a in child.select('a[href]')):
            return unknown
        residual = re.sub(r'共\s*\d+\s*条记录|每页\s*\d+\s*条|第\s*页|' + pager_labels,
                          '', child.get_text(' ', strip=True))
        if re.sub(r'[\s,，:：/|;；.·()（）-]+', '', residual) or child.select('img,iframe,object'):
            return unknown
    if any(child.name not in (item_tag, 'script', 'style') for child in listing.find_all(recursive=False)):
        return unknown
    current = layout.select(controls)
    if len(current) != 1:
        return unknown
    link = current[0]
    for node in [link, *link.parents]:
        style = re.sub(r'\s+', '', node.get('style', '')).lower()
        if node.has_attr('hidden') or node.get('aria-hidden') == 'true' or node.has_attr('inert') or any(
                marker in style for marker in ('display:none', 'visibility:hidden')):
            return unknown
    name = clean(link.get_text(' ', strip=True))
    target = resolve_page_link(url, link['href'])
    if not target or ARTICLE.search(target) or name in MORE | SKIP or not 2 <= len(name) <= 40:
        return unknown
    alias = None
    if target != canonical_url(url):
        from .publication_lists import listing_alias
        alias = listing_alias(listing, link, canonical_url(url), target)
        if not alias:
            return unknown
    return dict(name=name, heading_locator=locator(link), column_url=target,
                column_link_locator=locator(link), heading_method='local_selected_self_link',
                **({'listing_alias': alias} if alias else {}))


def zcms_active_column_heading(item, url):
    """A marked sidebar self-link names one complete adjacent ZCMS listing."""
    from .structure import ARTICLE, MORE, SKIP, clean, locator
    listing = item.find_parent('div', class_='SecondArticle__art')
    if listing is None or listing.parent is None or listing.parent.parent is None:
        return None
    right, layout = listing.parent, listing.parent.parent
    if ('c-main__right' not in right.get('class', [])
            or 'c-main--center' not in layout.get('class', [])):
        return None
    unknown = dict(name='', heading_locator='', column_url='', column_link_locator='',
                   heading_method='current_sidebar_self_link', heading_ambiguous=True)
    if (len(layout.select(':scope > .c-main__right')) != 1
            or len(right.select(':scope > .SecondArticle__art')) != 1
            or any(not any(p is listing for p in a.parents) for a in right.select('a[href]'))):
        return unknown
    current = layout.select(':scope > .c-main__left > .SecondLeft-nav__left > '
                            '.SecondLeft-nav__left--nav > .SecondLeft-nav__left--navClick > a[href]')
    if len(current) != 1:
        return unknown
    link = current[0]
    name = clean(link.get_text(' ', strip=True))
    target = resolve_page_link(url, link['href'])
    if not target or ARTICLE.search(target) or name in MORE | SKIP or not 2 <= len(name) <= 40:
        return unknown
    # The separate mobile control confirms the same label, but its '#' href
    # alone never supplies the canonical column address.
    mobile = layout.select(':scope > .SecondTop-nav__top > .SecondTop-nav__top--nav > '
                           '.SecondTop-nav__top--navClick > a')
    if mobile and (len(mobile) != 1 or clean(mobile[0].get_text(' ', strip=True)) != name):
        return unknown
    alias = None
    if target != canonical_url(url):
        from .publication_lists import listing_alias
        alias = listing_alias(listing, link, canonical_url(url), target)
        if not alias:
            return unknown
    return dict(name=name, heading_locator=locator(link), column_url=target,
                column_link_locator=locator(link), heading_method='current_sidebar_self_link',
                **({'listing_alias': alias} if alias else {}))


def zonghe_module_heading(item, url):
    """Bind a ZCMS module's literal title to its own list and optional banner."""
    from .structure import ARTICLE, MORE, SKIP, clean, locator
    module = item.find_parent(class_='c-zonghe-module__half-width')
    if module is None:
        return None
    unknown = dict(name='', heading_locator='', column_url='', column_link_locator='',
                   heading_method='adjacent_module_title', heading_ambiguous=True)
    heads = module.select(':scope > .c-zonghe-module__head')
    details = module.select(':scope > .c-zonghe-module__detail')
    titles = module.select(':scope > .c-zonghe-module__head > '
                           '.c-zonghe-module__head-title-container > '
                           '.c-zonghe-module__head-title-color')
    links = module.select(':scope > .c-zonghe-module__head > '
                          '.c-zonghe-module__head-more > a[href]')
    if len(heads) != 1 or len(details) != 1 or len(titles) != 1 or len(links) != 1:
        return unknown
    children = details[0].find_all(recursive=False)
    blocks = [child for child in children if child.name == 'ul' or
              'c-zonghe-module__banner-box' in child.get('class', [])]
    if (not blocks or any(child.name not in ('script', 'style') and
                         not any(child is block for block in blocks) for child in children)
            or not any(block is ancestor for block in blocks for ancestor in [item, *item.parents])
            or len([block for block in blocks if block.name == 'ul']) != 1):
        return unknown
    title = titles[0]
    name = clean(title.get_text(' ', strip=True))
    target = resolve_page_link(url, links[0]['href'])
    if (not 2 <= len(name) <= 40 or name in MORE | SKIP or not target or ARTICLE.search(target)
            or title.select('a[href],button,[role="tab"],[onclick],[onmouseover]')):
        return unknown
    return dict(name=name, heading_locator=locator(title), column_url=target,
                column_link_locator=locator(links[0]), heading_method='adjacent_module_title')


def zcms_notice_heading(item, url):
    """Use the terminal self-link for a sole adjacent ZCMS notice list."""
    from .structure import ARTICLE, clean, locator, MORE, SKIP
    listing = (item if item.name == 'ul' and 'middleNotice__notice' in item.get('class', [])
               else item.find_parent('ul', class_='middleNotice__notice'))
    if listing is None or listing.parent is None or listing.parent.parent is None:
        return None
    block, module = listing.parent, listing.parent.parent
    if 'middleNotice' not in block.get('class', []) or 'c-main__right' not in module.get('class', []):
        return None
    result = {'name': '', 'heading_locator': '', 'column_url': '', 'column_link_locator': '',
              'heading_method': 'current_page_breadcrumb', 'heading_ambiguous': True}
    crumbs = module.select(':scope > .c-main__title-container > .c-main__cur')
    if len(crumbs) != 1 or len(module.select(':scope > .middleNotice')) != 1:
        return result
    # A second widget cannot borrow this page-level crumb, even when it uses
    # opaque article URLs or a different card/list layout.
    allowed = [crumbs[0], listing, *module.select(':scope > .middleNotice__paging')]
    if any(not any(root is ancestor for root in allowed for ancestor in [link, *link.parents])
           for link in module.select('a[href]')):
        return result
    links = crumbs[0].select('a[href]')
    if not links:
        return result
    current = links[-1]
    name = clean(current.get_text(' ', strip=True))
    target = resolve_page_link(url, current['href'])
    if not target or not 2 <= len(name) <= 40 or name in MORE | SKIP or ARTICLE.search(target):
        return result
    alias = None
    if target != canonical_url(url):
        from .publication_lists import listing_alias
        alias = listing_alias(block, current, canonical_url(url), target)
        if not alias:
            return result
    # Earlier crumb labels may link to the same page. They remain visual
    # context, not another column identity or an organization parent.
    return {'name': name, 'heading_locator': locator(current), 'column_url': target,
            'column_link_locator': locator(current),
            'heading_method': 'pagination_page_breadcrumb' if alias else 'current_page_breadcrumb',
            **({'listing_alias': alias} if alias else {})}


def rightnew_heading(item, url):
    from .structure import clean, locator, MORE, SKIP
    listing = item.find_parent('ul', class_='rightnew')
    if listing is None or listing.parent is None or listing.parent.parent is None:
        return None
    block, module = listing.parent, listing.parent.parent
    if 'xnewlist' not in block.get('class', []) or 'xxright' not in module.get('class', []):
        return None
    result = {'name': '', 'heading_locator': '', 'column_url': '', 'column_link_locator': '',
              'heading_method': 'current_page_breadcrumb'}
    headings = module.select(':scope > .righttop > span:not(.ben)')
    crumbs = module.select(':scope > .righttop > span.ben')
    links = crumbs[0].select('a[href]') if len(crumbs) == 1 else []
    if len(headings) != 1 or not links:
        return dict(result, heading_ambiguous=True)
    name = clean(headings[0].get_text(' ', strip=True))
    current = links[-1]
    if (name != clean(current.get_text(' ', strip=True)) or not 2 <= len(name) <= 40 or name in MORE | SKIP
            or resolve_page_link(url, current['href']) != canonical_url(url)):
        return dict(result, heading_ambiguous=True)
    result.update(name=name, heading_locator=locator(headings[0]), column_url=canonical_url(url),
                  column_link_locator=locator(current))
    if len(links) > 1:
        parent = links[-2]
        label = clean(parent.get_text(' ', strip=True))
        if label and label not in MORE | SKIP and resolve_page_link(url, parent['href']):
            result.update(column_group_name=label, column_group_locator=locator(parent))
    return result
