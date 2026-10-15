"""Compare installed columns by executing their rules on one verified full page."""
from bs4 import BeautifulSoup
from soupsieve.util import SelectorSyntaxError

from backend.services.source_inventory import canonical_url


def same_publisher(source, config):
    from backend.services.source_governance import _normal
    return _normal(source.group_name or '') == _normal(config.get('group_name', ''))


def same_observed_column(source, config, validation):
    """Equivalent CSS must select the same complete widget, never just samples.

    Installation has already validated the new candidate. Re-execute both rules
    against that exact retained page and its verified heading/container. This
    comparison grants no publisher identity and does not prove a relocation.
    """
    from backend.services.source_governance import (_column_scope, _column_label,
        _normal, read_snapshot, source_config)
    from backend.scraper.discovery.publication_lists import select_node
    if not same_publisher(source, config):
        return False
    if canonical_url(source.list_url) != canonical_url(config['list_url']):
        return False
    if _normal(_column_label(source.name, source.group_name or '')) != _normal(
            _column_label(config['name'], config.get('group_name', ''))):
        return False
    reference = validation.get('list_snapshot') or {}
    region = validation.get('region') or {}
    if (validation.get('status') not in ('full', 'partial') or not region.get('container')
            or not region.get('heading') or canonical_url(reference.get('url', '')) != canonical_url(config['list_url'])):
        return False
    scope = {'container_selector': region['container'], 'heading_selector': region['heading']}
    previous = source_config(source)
    try:
        html = read_snapshot(reference)
        records, errors, current_region = _column_scope(config, reference, scope, html=html)
        old_records, old_errors, old_region = _column_scope(previous, reference, scope, html=html)
        if (errors or old_errors or not records or old_records != records
                or old_region.get('container') != current_region.get('container')
                or old_region.get('heading') != current_region.get('heading')):
            return False
        soup = BeautifulSoup(html, 'lxml')
        items, old_items = soup.select(config['list_selector']), soup.select(previous['list_selector'])
        if not items or [id(item) for item in items] != [id(item) for item in old_items]:
            return False
        # Rules for a neighbouring link/title inside a shared card are distinct.
        for item in items:
            for field, default in (('link_selector', 'a[href]'), ('title_selector', '')):
                new_node = select_node(item, config.get(field) or default) if config.get(field) or default else item
                old_node = select_node(item, previous.get(field) or default) if previous.get(field) or default else item
                if new_node is None or new_node is not old_node:
                    return False
    except (KeyError, ValueError, TypeError, OSError, SelectorSyntaxError):
        return False
    return True
