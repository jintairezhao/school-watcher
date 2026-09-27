"""Plain-text source paths backed by the same evidence as the inbox tree."""
from flask import has_request_context, request

from backend.services.source_placements import official_source_placements


def source_breadcrumb(source):
    """Keep saved source IDs/names intact and let Jinja escape every path part."""
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
    return ' / '.join(str(part) for part in parts if part)
