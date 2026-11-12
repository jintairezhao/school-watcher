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
    from backend.services.source_governance import _column_label, _normal, source_config
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
    previous = source_config(source)
    if alias:
        previous = dict(previous, list_url=config['list_url'])
    return same_column_selection(previous, config, reference, region)


def same_column_selection(previous, config, reference, region):
    """Compare exact current DOM selections; this alone grants no identity.

    Also usable while a proposal is being validated. A caller may constrain an
    existing verified publisher with this result, but must still validate the
    new proposal and cannot treat matching page content as a publisher claim.
    """
    from backend.services.source_governance import _column_scope, read_snapshot
    from backend.scraper.discovery.publication_lists import select_node
    if (not region.get('container') or not region.get('heading')
            or canonical_url(reference.get('url', '')) != canonical_url(config['list_url'])
            or canonical_url(previous.get('list_url', '')) != canonical_url(config['list_url'])):
        return False
    scope = {'container_selector': region['container'], 'heading_selector': region['heading']}
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


def column_observation(config, receipt, html):
    """Fingerprint an executed selection for counting, never a persistent source ID.

    The caller supplies a current gated receipt and its exact retained full DOM.
    Equivalent CSS selects the same nodes; neighbouring widgets or child links
    keep separate observations even when names and article URLs happen to match.
    Publisher labels constrain this comparison but grant no ownership claim.
    """
    from backend.services.source_governance import _hash, _normal, _column_label, _column_scope
    from backend.scraper.discovery.publication_lists import select_node
    from backend.scraper.discovery.structure import locator
    if (not isinstance(html, str) or not html or _hash(html) != receipt.get('content_hash')
            or _hash(config) != receipt.get('config_hash')
            or canonical_url(config['list_url']) != receipt.get('url')):
        return None
    region = receipt.get('region') or {}
    if not all(isinstance(region.get(k), str) and region[k].strip() for k in ('container','heading')):
        return None
    reference = {'url':receipt['url'], 'hash':receipt['content_hash'], 'outcome':'usable'}
    scope = dict(container_selector=region['container'], heading_selector=region['heading'])
    try:
        records, errors, observed = _column_scope(config, reference, scope, html=html)
        if errors:
            return None
        soup = BeautifulSoup(html, 'lxml')
        containers, headings = soup.select(observed.get('container','')), soup.select(observed.get('heading',''))
        items = soup.select(config['list_selector'])
        if len(containers) != 1 or len(headings) != 1 or not items:
            return None
        selected = []
        for item in items:
            title = select_node(item, config['title_selector']) if config.get('title_selector') else item
            link = select_node(item, config.get('link_selector') or 'a[href]')
            if title is None or link is None:
                return None
            selected.append([locator(item), locator(title), locator(link)])
        fingerprint = _hash(dict(url=reference['url'], content_hash=reference['hash'],
            publisher=_normal(config.get('group_name','')),
            name=_normal(_column_label(config['name'], config.get('group_name',''))),
            container=locator(containers[0]), heading=locator(headings[0]), selected=selected, records=records))
    except (ValueError, TypeError, KeyError, SelectorSyntaxError):
        return None
    return dict(schema=1, basis='executed_full_dom_selection', fingerprint=fingerprint,
        source_id=receipt['source_id'], url=reference['url'], content_hash=reference['hash'],
        config_hash=receipt['config_hash'])


def receipt_count_identity(receipt):
    """Use only an annotation bound to this exact source/config/material receipt."""
    import re
    observed = receipt.get('column_observation')
    if (isinstance(observed, dict) and observed.get('schema') == 1
            and observed.get('basis') == 'executed_full_dom_selection'
            and isinstance(observed.get('fingerprint'), str)
            and re.fullmatch('[0-9a-f]{64}', observed['fingerprint'])
            and all(observed.get(k) == receipt.get(k) for k in ('source_id','url','content_hash','config_hash'))):
        return 'column:' + observed['fingerprint']
    return str(receipt['source_id'])
