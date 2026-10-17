"""Order all observed navigation candidates before a new AI decision sequence.

This is an attention hint, not a role verdict. Candidate objects and their
observation/cache material remain untouched. The caller owns checkpoints and
must never apply a new order to a resumed or possibly paid decision sequence.
"""
from collections.abc import Mapping
import json
import re

from .attention import candidate_attention, complete_material
from .directory_terms import academic_collection_label
from backend.services.student_sources import COLLEGE_DIRECTORY


_ACADEMIC_ENTRANCE = re.compile(
    r'(?:院系(?:设置|机构)?|学院设置|教学(?:机构|单位)|教学科研单位|学部设置|二级学院|'
    r'(?:本科|研究生)?(?:学科|专业)(?:设置|目录|介绍|建设)|学科[、与和/\s]*专业(?:设置|目录)?)')
_ORGANIZATION_ENTRANCE = re.compile(r'组织机构|机构设置|管理机构|职能部门|行政机构|学校机构')
_DIRECTORY_PLACEMENTS = frozenset((
    'directory_entry', 'nested_directory_entry', 'same_table_row', 'same_directory_row',
    'attached_unit', 'sub_unit', 'table_reference', 'non_entity_directory_entry',
))


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _path(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    return [part for part in value if isinstance(part, str)] if isinstance(value, (list, tuple)) else []


def _directory_member(link, page):
    # Reused site navigation is not the directory's own roster. Bind the
    # parser's observed local placement by both URL and concrete locator.
    nodes = page.get('observed_structure') or []
    return bool((page.get('kind') == 'directory' or academic_collection_label(page.get('label')))
                and isinstance(nodes, (list, tuple))
                and link.get('locator') and any(
                isinstance(node, Mapping) and node.get('url') == link.get('url')
                and node.get('locator') == link['locator']
                and node.get('relation') in _DIRECTORY_PLACEMENTS
                for node in nodes))


def _trunk_prior(link, page, has_need):
    """A finite root-page layout pass; never turn a news item into a directory."""
    if page.get('kind') == 'directory' or academic_collection_label(page.get('label')):
        if _directory_member(link, page):
            return (0 if has_need else 2), 'observed_directory_member_hint'
        return (1 if has_need else 3), None
    if page.get('kind') != 'root' or link.get('publication_route') or link.get('kind') == 'channel':
        return 3, None
    label, kind = _text(link.get('label')), link.get('kind')
    if (kind in ('directory', 'unit', 'navigation', 'gateway') and academic_collection_label(label)
            or kind == 'directory' and COLLEGE_DIRECTORY.search(label)
            or kind in ('navigation', 'gateway') and _ACADEMIC_ENTRANCE.fullmatch(label)):
        return 0, 'observed_academic_layout_hint'
    if (kind == 'directory' and _ORGANIZATION_ENTRANCE.search(label)
            or kind in ('navigation', 'gateway') and _ORGANIZATION_ENTRANCE.fullmatch(label)):
        return 1, 'observed_organization_layout_hint'
    if kind == 'directory':
        return 2, 'observed_other_layout_hint'
    return 3, None


def explain_candidates(candidates, page, attention=None):
    """Return sorted explanations without changing any candidate or context.

    Whole-menu ``text`` and ``quote_text`` are kept for the actual AI evidence,
    but not fed to this small priority hint. A free-form local line is used only
    when it names this candidate, no other candidate, and one unambiguous need.
    Multi-need structured ancestry remains usable. Ambiguous text is retained
    unchanged in the caller's input, rather than erased or treated as fact.
    """
    page = page if isinstance(page, Mapping) else {}
    attention = {} if attention is None else attention
    if not isinstance(attention, Mapping):
        raise ValueError('Navigation attention must be a configuration mapping')
    settings = {'profiles': attention.get('profiles'), 'weights': attention.get('weights')}
    # Reject malformed explicit preferences even if the candidate list is empty.
    candidate_attention({}, **settings)
    candidates = list(candidates)
    for candidate in candidates:
        if (not isinstance(candidate, (list, tuple)) or len(candidate) != 2
                or not isinstance(candidate[0], Mapping)):
            raise ValueError('Navigation candidates must contain a link and observed context')
    labels = {_text(link.get('label')) for link, _ in candidates}
    result = []
    for original_index, (link, context) in enumerate(candidates):
        material = link.get('_navigation_material')
        material = material if isinstance(material, Mapping) else {}
        hierarchy = _path(material.get('hierarchy'))
        route = {'url': _text(link.get('url')), 'label': _text(link.get('label')),
                 'kind': link.get('kind'), 'path': [*_path(link.get('path', link.get('path_json'))), *hierarchy]}
        info = candidate_attention(route, **settings)
        evidence, gains = list(info['evidence']), dict(info['gains'])
        basis = ['observed_link']
        if route['path']:
            basis.append('observed_ancestry')

        def add_context(text, field):
            extra = candidate_attention({'url': route['url'], 'label': text}, **settings)
            evidence.extend(dict(item, field=field, url=_text(page.get('url')),
                                 locator=_text(link.get('locator')),
                                 content_hash=_text(page.get('content_hash'))) for item in extra['evidence'])
            for category, gain in extra['gains'].items():
                gains.setdefault(category, gain)
            return extra['needs']

        if page.get('kind') in ('unit', 'gateway', 'channel') and complete_material(page):
            for field in ('label', 'title'):
                if _text(page.get(field)):
                    add_context(page[field], 'observed_parent_' + field)
            basis.append('observed_parent_identity')
        elif _directory_member(link, page) and complete_material(page):
            # A literal department name such as 计算机系 need not repeat the
            # directory's 院系设置 heading. Only its bound roster entries inherit
            # this demand context; repeated header menus do not.
            add_context(_text(page.get('label')), 'observed_directory_identity')
            basis.append('observed_directory_identity')
        local_line = context.splitlines()[-1].strip() if isinstance(context, str) and context.strip() else ''
        other_labels = [label for label in labels if label and label != route['label']
                        and label not in route['label'] and route['label'] not in label]
        if (route['label'] and route['label'] in local_line and local_line != route['label']
                and not any(label in local_line for label in other_labels)):
            local = candidate_attention({'url': route['url'], 'label': local_line}, **settings)
            if len(local['needs']) == 1:
                add_context(local_line, 'observed_local_context')
                basis.append('isolated_local_context')
            elif local['needs']:
                basis.append('mixed_local_context_not_ranked')
        elif local_line and local_line != route['label']:
            basis.append('unbound_or_shared_context_not_ranked')
        score = sum(gains.values())
        prior, trunk_reason = _trunk_prior(link, page, bool(gains))
        result.append({'original_index': original_index, 'url': route['url'], 'label': route['label'],
                       'rank': (prior, -score, original_index), 'score': score,
                       'needs': tuple(dict.fromkeys(item['category'] for item in evidence)),
                       'evidence': evidence, 'context_basis': basis,
                       'reason': trunk_reason or ('observed_profile_need' if gains
                                                  else 'other_observed_candidate_retained')})
    return sorted(result, key=lambda row: row['rank'])


def order_candidates(candidates, page, attention=None):
    """Return every original pair in the explainable order, with stable ties."""
    candidates = list(candidates)
    return [candidates[row['original_index']] for row in explain_candidates(candidates, page, attention)]
