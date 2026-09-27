"""Reviewed MetInfo listing layout, with alternating decorative row classes."""
from backend.services.source_inventory import canonical_url, resolve_page_link
from .structure import clean, locator, MORE, SKIP


METINFO_CONFIG = {
    'list_selector': '#news_right > .news_center > ul > li.news_center_li',
    'title_selector': 'a', 'link_selector': 'a', 'date_selector': 'span',
    'content_selector': 'article, .editor',
    'method': 'official_metinfo_list', 'confidence': .95,
}


def metinfo_heading(item, url):
    listing = item.parent
    block = listing.parent if listing is not None else None
    section = block.parent if block is not None else None
    if (item.name != 'li' or 'news_center_li' not in item.get('class', [])
            or listing.name != 'ul' or 'news_center' not in block.get('class', [])
            or section is None or section.get('id') != 'news_right'):
        return None
    result = {'name': '', 'heading_locator': '', 'column_url': '', 'column_link_locator': ''}
    headings = section.select(':scope > h2')
    if len(headings) != 1:
        return dict(result, heading_ambiguous=True)
    heading = headings[0]
    label = clean(' '.join(heading.find_all(string=True, recursive=False)))
    links = heading.select(':scope > span a[href]')
    current = links[-1] if links else None
    if (not 2 <= len(label) <= 40 or label in MORE | SKIP or current is None
            or clean(current.get_text(' ', strip=True)) != label
            or resolve_page_link(url, current['href']) != canonical_url(url)):
        return dict(result, heading_ambiguous=True)
    return dict(result, name=label, heading_locator=locator(heading),
                column_url=canonical_url(url), column_link_locator=locator(current),
                heading_method='current_page_breadcrumb')
