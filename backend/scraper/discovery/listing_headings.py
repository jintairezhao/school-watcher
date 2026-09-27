"""Current-page breadcrumb evidence for reviewed listing layouts."""
from backend.services.source_inventory import canonical_url, resolve_page_link


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
