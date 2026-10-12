"""Human labels are never addresses; addresses remain separate link targets.

This is a read-only presentation guard for legacy records, not ownership proof.
Missing identity must not prevent collecting an otherwise usable source.
"""
import re
from flask import has_request_context, request

from backend.services.source_placements import official_source_placements

_ADDRESS = re.compile(r'https?://|(?:\b[a-z0-9-]+\.)+[a-z]{2,}(?:[/?:#]|\b)', re.I)


def source_label(value, fallback=''):
    label = (value or '').strip()
    return fallback if not label or _ADDRESS.search(label) else label


def source_breadcrumb(source):
    """Keep saved IDs intact and let Jinja escape every human path part."""
    if source is None:
        return ''
    placements = official_source_placements([source]).get(source.id, [])
    if placements:
        requested_group = request.args.get('group', '') if has_request_context() else ''
        placement = next((p for p in placements if p['group'] == requested_group), placements[0])
        parts = [placement['group'], *(node['name'] for node in placement['nodes']),
                 placement['label']]
    else:
        parts = [source.group_name, source.name]
    return ' / '.join(label for part in parts if (label := source_label(part))) or '官网栏目'
