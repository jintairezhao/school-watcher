"""Observed CMM homepage widgets; their labels and addresses come from the page."""
import re
from urllib.parse import urlsplit

from backend.services.source_inventory import resolve_page_link, canonical_url
from backend.scraper.article_urls import ARTICLE

# Module, CMS widget, row selector, title, link, publication date.
WIDGETS = (
    ('.first-news-box', 'wp_news_w3', 'ul > li.items', '.texts > h3', '.picimg > a', '.texts > .news-time'),
    ('.first-news-box', 'wp_news_w4', 'ul > li.items', '.texts h3 > a', '.texts h3 > a', '.texts > .news-time'),
    ('.second-notice-box .ft-block', 'wp_news_w5', 'ul > li.items', '.texts h3 > a', '.texts h3 > a', '.time'),
    ('.second-notice-box .rt-block', 'wp_news_w6', 'ul > li.items', '.rt h3 > a', '.rt h3 > a', ''),
    ('.add-research-box', 'wp_news_w7', 'ul > li.items', '.texts > a', '.texts > a', ''),
    ('.add-communication-box .rt-block', 'wp_news_w9', 'ul > li.items', '.texts h3 > a', '.texts h3 > a', '.time'),
    ('.ft-audio-visual', 'wp_news_w11', ':scope > h3', 'a', 'a', ''),
)


def medical_home(url):
    page = urlsplit(url)
    return page.hostname == 'www.cmm.zju.edu.cn' and page.path in ('/', '/main.htm')


def medical_heading(item, url):
    listing = medical_list_heading(item, url)
    if listing:
        return listing
    if not medical_home(url):
        return None
    import soupsieve
    from .structure import clean, locator, MORE
    selectors = {row[0] for row in WIDGETS} | {'.ft-media-look'}
    for block in [item, *item.parents]:
        if not any(soupsieve.match(selector, block) for selector in selectors):
            continue
        headings = block.select('.rt-text > h3')
        links = [a for a in block.select('a[href]') if clean(a.get_text(' ', strip=True)) in MORE]
        targets = {resolve_page_link(url, a.get('href')) for a in links}
        targets.discard('')
        if len(headings) != 1 or len(targets) != 1:
            return None
        target = next(iter(targets))
        name = clean(headings[0].get_text(' ', strip=True))
        if ARTICLE.search(target) or not 2 <= len(name) <= 40:
            return None
        link = next(a for a in links if resolve_page_link(url, a.get('href')) == target)
        return {'name': name, 'heading_locator': locator(headings[0]), 'column_url': target,
                'column_link_locator': locator(link), 'heading_method': 'official_medical_module'}
    return None


def medical_configs(soup, url):
    """Pin date semantics as well as list boundaries; event time is not published time."""
    if not medical_home(url):
        return medical_list_configs(soup, url)
    from .structure import locator
    configs, reviewed_widgets = [], set()
    for module, widget_id, rows, title, link, date in WIDGETS:
        widgets = soup.select(module + ' #' + widget_id)
        if len(widgets) != 1:
            continue
        widget = widgets[0]
        items = widget.select(rows)
        if not items or medical_heading(items[0], url) is None:
            continue
        # Full locators preserve separate widgets even on pages with duplicate IDs.
        selector = locator(widget) + (' > h3' if rows.startswith(':scope') else ' > ' + rows)
        configs.append({'list_selector': selector, 'title_selector': title, 'link_selector': link,
                        'date_selector': date, 'content_selector': 'div.wp_articlecontent, article',
                        'method': 'official_medical_module', 'confidence': 1.0, 'minimum_items': 1,
                        'explicit_video_publication': widget_id == 'wp_news_w11' and widget.find('video') is not None})
        reviewed_widgets.add(id(widget))
    return configs, reviewed_widgets


def medical_list_section(url):
    page = urlsplit(url)
    match = re.fullmatch(r'/(38670|kycx|rczp|38678|38677|stzy|mtkzy)/list(?:[1-9]\d*)?\.htm', page.path)
    return match[1] if page.hostname == 'www.cmm.zju.edu.cn' and match else None


def medical_list_heading(item, url):
    section = medical_list_section(url)
    if not section:
        return None
    from .structure import clean, locator, MORE, SKIP
    main = item.find_parent('section', class_='news-content-box')
    if main is None:
        return None
    if section == 'rczp':
        group = item.find_parent('li', class_='wp_sublist')
        if group is None:
            return None
        headings = group.select(':scope > h3.sublist_title > a[childcolumnid]')
        if len(headings) != 1:
            return None
        heading = headings[0]
        name = clean(heading.get_text(' ', strip=True))
        target = resolve_page_link(url, heading.get('href'))
        if not name or not target or ARTICLE.search(target):
            return None
        return {'name': name, 'heading_locator': locator(heading), 'column_url': target,
                'column_link_locator': locator(heading), 'heading_method': 'official_medical_recruitment_group'}
    document = main
    while document.parent is not None:
        document = document.parent
    if document.title is None:
        return None
    name = clean(document.title.get_text(' ', strip=True))
    if not 2 <= len(name) <= 40 or name in MORE | SKIP:
        return None
    # The banner can name a broader parent (新闻公告 / 综合服务). The reviewed
    # listing document's own title is the current column, not that parent.
    selected = [a for a in document.select('.nav-menu .selected > a[href]')
                if resolve_page_link(url, a.get('href')) == canonical_url(url)
                and clean(a.get_text(' ', strip=True)) == name]
    heading = selected[0] if len(selected) == 1 else document.title
    return {'name': name, 'heading_locator': locator(heading), 'column_url': canonical_url(url),
            'column_link_locator': locator(heading) if selected else 'document',
            'heading_method': 'current_medical_list_document'}


def medical_list_configs(soup, url):
    section = medical_list_section(url)
    if not section:
        return [], set()
    from .structure import locator
    specs = {
        '38670': [('ul.collegenews-ul', '.texts > h3', '.picimg > a', '.texts > .news-time'),
                  ('ul.collegenews-ul-2', '.texts h3 > a', '.texts h3 > a', '.texts > .news-time')],
        'kycx': [('ul.news-ul', '.rttext h3 > a', '.rttext h3 > a', '.fttime')],
        '38678': [('ul.news-ul', '.rttext h3 > a', '.rttext h3 > a', '.fttime')],
        'mtkzy': [('ul.news-ul', '.rttext h3 > a', '.rttext h3 > a', '.fttime')],
        '38677': [('ul.academic-ul', '.top-block .rttext > a', '.top-block .rttext > a', '')],
        'stzy': [('ul.news-ul', '.rttext h3 > a[title]', '.rttext h3 > a[title]', '')],
        'rczp': [('li.wp_sublist > ul.bottom', 'span.title > a', 'span.title > a', 'span.time')],
    }
    configs, reviewed = [], set()
    for selector, title, link, date in specs[section]:
        for listing in soup.select('.news-content-box ' + selector):
            rows = listing.find_all('li', recursive=False)
            if not rows or medical_list_heading(rows[0], url) is None:
                continue
            configs.append({'list_selector': locator(listing) + ' > li', 'title_selector': title,
                            'link_selector': link, 'date_selector': date, 'content_selector': 'div.wp_articlecontent, article',
                            'method': 'official_medical_module', 'confidence': 1.0, 'minimum_items': 1,
                            'explicit_video_publication': section == 'stzy' and all(row.find('video') is not None for row in rows)})
            reviewed.add(id(listing))
    return configs, reviewed


def medical_article_locators(soup, url):
    """Publication titles inside nested recruitment lists are not institution menus."""
    from .structure import locator
    configs, _ = medical_list_configs(soup, url)
    return {locator(anchor) for config in configs for row in soup.select(config['list_selector'])
            for anchor in row.select(config['link_selector'])}
