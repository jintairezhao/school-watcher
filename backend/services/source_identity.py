"""Compare installed columns by executing their rules on one verified full page."""
from bs4 import BeautifulSoup
from soupsieve.util import SelectorSyntaxError
import json
from urllib.parse import urljoin, urlsplit

from backend.services.source_inventory import canonical_url


def same_publisher(source, config):
    from backend.services.source_governance import _normal
    return _normal(source.group_name or '') == _normal(config.get('group_name', ''))


def observed_home_aliases(validation):
    """Candidate addresses only; reuse still requires both full observations.

    A directory and its explicitly linked home document may be the same page.
    Never globally normalize default filenames, query strings or migrations.
    """
    from backend.services.source_governance import read_snapshot, _normal
    reference = validation.get('list_snapshot') or {}
    address = canonical_url(reference.get('url', ''))
    parsed = urlsplit(address)
    if not address or parsed.query or parsed.fragment:
        return {address} if address else set()
    directory = urljoin(address, './')
    aliases = {address}
    try:
        soup = BeautifulSoup(read_snapshot(reference), 'lxml')
        if soup.select('base[href]'):
            return aliases
        for link in soup.select('a[href]'):
            target = canonical_url(urljoin(address, link['href']))
            if (_normal(link.get_text(' ', strip=True)).casefold() not in ('首页', 'home')
                    or urlsplit(target).query or urlsplit(target).fragment
                    or urljoin(target, './') != directory or target == directory):
                continue
            if address in (directory, target):
                aliases.update((directory, target))
    except (ValueError, OSError):
        pass
    return aliases


def _same_home_observation(source, config, validation):
    """An actual self-home link and identical complete HTML prove this alias."""
    from backend.database.source_governance_models import SourceValidation, SourceProposal
    from backend.services.source_governance import read_snapshot
    before = canonical_url(source.list_url)
    if before not in observed_home_aliases(validation):
        return False
    current = validation.get('list_snapshot') or {}
    def previous_observations():
        for event in SourceValidation.query.filter_by(department_id=source.id).order_by(SourceValidation.id.desc()):
            yield json.loads(event.validation_json)
        for proposal in SourceProposal.query.filter_by(department_id=source.id, state='activated'):
            if json.loads(proposal.validation_json or '{}').get('passed'):
                yield {'status': 'full', 'articles': {'passed': True},
                       'list_snapshot': json.loads(proposal.evidence_json).get('list', {})}
    for old in previous_observations():
        reference = old.get('list_snapshot') or {}
        if (canonical_url(reference.get('url', '')) != before
                or old.get('status') not in ('full', 'partial', 'limited')
                or not old.get('articles', {}).get('passed')
                or not reference.get('hash') or reference['hash'] != current.get('hash')):
            continue
        try:
            return read_snapshot(reference) == read_snapshot(current)
        except (ValueError, OSError):
            return False
    return False


def same_observed_column(source, config, validation):
    """Equivalent CSS must select the same complete widget, never just samples.

    Installation has already validated the new candidate. Re-execute both rules
    against that exact retained page and its verified heading/container. This
    comparison grants no publisher identity and does not prove a relocation.
    A separately observed identical self-home document can supply a page alias.
    """
    from backend.services.source_governance import (_column_scope, _column_label,
        _normal, read_snapshot, source_config)
    from backend.scraper.discovery.publication_lists import select_node
    if not same_publisher(source, config):
        return False
    alias = canonical_url(source.list_url) != canonical_url(config['list_url'])
    if alias and not _same_home_observation(source, config, validation):
        return False
    if _normal(_column_label(source.name, source.group_name or '')) != _normal(
            _column_label(config['name'], config.get('group_name', ''))):
        return False
    reference = validation.get('list_snapshot') or {}
    region = validation.get('region') or {}
    accepted = validation.get('status') in ('full', 'partial') or (
        validation.get('status') == 'limited' and validation.get('articles', {}).get('passed') is True)
    if (not accepted or not region.get('container')
            or not region.get('heading') or canonical_url(reference.get('url', '')) != canonical_url(config['list_url'])):
        return False
    scope = {'container_selector': region['container'], 'heading_selector': region['heading']}
    previous = source_config(source)
    if alias:
        previous = dict(previous, list_url=config['list_url'])
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
