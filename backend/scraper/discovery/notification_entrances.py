"""Observed homepage notification blocks guide exploration, never ownership."""
from collections.abc import Mapping
import json
import re

from backend.services.source_inventory import canonical_url
from .attention import complete_material

LABELS = frozenset(('通知公告', '通知通告', '通知', '公告', '重要通知', '公告通知'))


def _url(value):
    if not isinstance(value, str) or not value:
        return ''
    try:
        return canonical_url(value)
    except ValueError:
        return ''


def notification_blocks(kind, reference_url, content_hash, publication):
    """Use observed main entries; leaf validation and source scopes stay separate."""
    if (kind != 'unit' or not _url(reference_url) or not isinstance(content_hash, str)
            or not re.fullmatch('[0-9a-f]{64}', content_hash) or not isinstance(publication, Mapping)):
        return []
    columns = publication.get('lists', [publication])
    if not isinstance(columns, (list, tuple)):
        return []
    result, grouped_entries = [], {}
    for column in columns:
        if not isinstance(column, Mapping):
            continue
        label = re.sub(r'\s+', '', column.get('name', '')) if isinstance(column.get('name'), str) else ''
        group = (re.sub(r'\s+', '', column.get('column_group_name', ''))
                 if isinstance(column.get('column_group_name'), str) else '')
        # A tabbed preview can have no verified leaf name while its literal
        # outer heading and actual more-link still prove one navigation entry.
        # This grants a route turn, never a merged or validated source scope.
        group_entry = (not label and group in LABELS and column.get('heading_group_reference') is True
                       and column.get('column_group_locator') == column.get('heading_locator'))
        if group_entry:
            label = group
        target = _url(column.get('column_url'))
        if (label not in LABELS or column.get('column_group_name') and not group_entry or not target
                or target == _url(reference_url) or not all(isinstance(column.get(field), str)
                and column[field] for field in ('container_locator', 'heading_locator', 'column_link_locator'))):
            continue
        key = (target, column['heading_locator'], column['column_link_locator'])
        if group_entry and key in grouped_entries:
            containers = grouped_entries[key]['preview_container_locators']
            if column['container_locator'] not in containers:
                containers.append(column['container_locator'])
            continue
        hint = dict(basis='observed_unit_home_notification', reference_url=_url(reference_url),
            content_hash=content_hash, column_url=target, label=label,
            container_locator=column['container_locator'], heading_locator=column['heading_locator'],
            entry_locator=column['column_link_locator'])
        if group_entry:
            hint.update(heading_basis='observed_notification_group_heading',
                        preview_container_locators=[column['container_locator']])
            grouped_entries[key] = hint
        result.append(hint)
    return result


def current_notification_hint(hints, parent, target_url, *, edge_locator=None):
    if parent.get('kind') != 'unit' or not complete_material(parent):
        return None
    if not isinstance(hints, (list, tuple)):
        return None
    address = _url(parent.get('final_url') or parent.get('url'))
    for hint in hints:
        if (isinstance(hint, Mapping) and hint.get('basis') == 'observed_unit_home_notification'
                and hint.get('reference_url') == address
                and hint.get('content_hash') == parent.get('content_hash')
                and hint.get('column_url') == _url(target_url) and hint.get('label') in LABELS
                and all(hint.get(field) for field in ('container_locator', 'heading_locator', 'entry_locator'))
                and (edge_locator is None or edge_locator == hint['entry_locator'])):
            return dict(hint)
    return None


def saved_notification_hint(parent, target_url, edge_locator):
    publication = parent.get('feed_json') or {}
    if isinstance(publication, str):
        try:
            publication = json.loads(publication)
        except (ValueError, TypeError):
            return None
    hints = notification_blocks(parent.get('kind'), parent.get('final_url') or parent.get('url'),
                                parent.get('content_hash'), publication)
    return current_notification_hint(hints, parent, target_url, edge_locator=edge_locator)


def saved_publication_hint(parent, target_url, edge_locator):
    """A literal preview/more-link proves a route, regardless of page entity kind.

    Unlike a faculty's generic main-notice heading, a differently named entry
    requires actual preview items. Interest is checked by the caller against
    this exact preview, never against a site-wide menu or a neighbouring list.
    This is neither target-page material nor a collecting/ownership receipt.
    """
    if not complete_material(parent) or not isinstance(edge_locator, str) or not edge_locator:
        return None
    reference = _url(parent.get('final_url') or parent.get('url'))
    target = _url(target_url)
    if not reference or not target or target == reference:
        return None
    publication = parent.get('feed_json') or {}
    if isinstance(publication, str):
        try:
            publication = json.loads(publication)
        except (ValueError, TypeError):
            return None
    if not isinstance(publication, Mapping):
        return None
    columns = publication.get('lists', [publication])
    if not isinstance(columns, (list, tuple)):
        return None
    for column in columns:
        if (not isinstance(column, Mapping) or _url(column.get('column_url')) != target
                or not isinstance(column.get('name'), str) or not column['name'].strip()
                or column.get('heading_ambiguous') or column.get('column_group_name')
                or not all(isinstance(column.get(field), str) and column[field].strip()
                           for field in ('container_locator', 'heading_locator', 'column_link_locator'))
                or column['column_link_locator'] != edge_locator):
            continue
        samples = column.get('samples') or []
        samples = samples if isinstance(samples, (list, tuple)) else ()
        items = {address for sample in samples if isinstance(sample, Mapping)
                 and isinstance(sample.get('title'), str) and sample['title'].strip()
                 and (address := _url(sample.get('url'))) and address not in (reference, target)}
        if items:
            return dict(basis='observed_publication_preview', reference_url=reference,
                content_hash=parent['content_hash'], column_url=target, label=column['name'].strip(),
                container_locator=column['container_locator'], heading_locator=column['heading_locator'],
                entry_locator=column['column_link_locator'], preview_item_count=len(items))
    return None
