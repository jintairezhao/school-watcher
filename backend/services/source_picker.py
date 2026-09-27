"""Inert evidence previews and server-derived selectors from administrator clicks."""
import json
from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.source_governance_models import SourceProposal
from backend.services.source_governance import read_snapshot, queue_source_review


def _document(proposal):
    reference = json.loads(proposal.evidence_json or '{}').get('list')
    if not reference:
        raise ValueError('尚未保存列表页证据，请先重新检查')
    soup = BeautifulSoup(read_snapshot(reference), 'lxml')
    nodes = {str(i): element for i, element in enumerate(soup.find_all(True), 1)}
    return soup, nodes, reference


def _path(node, stop=None):
    parts = []
    while node is not None and node.name != '[document]' and node is not stop:
        siblings = node.parent.find_all(node.name, recursive=False) if node.parent else [node]
        position = next(i for i, sibling in enumerate(siblings, 1) if sibling is node)
        parts.append(f'{node.name}:nth-of-type({position})')
        node = node.parent
    return ' > '.join(reversed(parts)) or ':scope'


def preview(proposal_id):
    proposal = db.session.get(SourceProposal, proposal_id)
    if proposal is None:
        raise ValueError('来源建议不存在')
    soup, nodes, reference = _document(proposal)
    for identity, node in nodes.items():
        node.attrs['data-pick-id'] = identity
    for node in list(soup.find_all(['script', 'style', 'link', 'meta', 'base', 'iframe', 'frame',
                                    'object', 'embed', 'img', 'video', 'audio', 'source', 'svg', 'math'])):
        node.decompose()
    for node in soup.find_all(True):
        identity = node.get('data-pick-id')
        node.attrs = {'data-pick-id': identity} if identity else {}
        if node.name in ('input', 'textarea', 'select', 'button'):
            node['disabled'] = ''
        if node.name == 'form':
            node.name = 'div'
    # All website active content/URLs are removed. The only executable code is
    # our picker below; iframe sandbox denies origin access and navigation.
    markup = str(soup.body or soup)
    return {'evidence_hash': reference['hash'], 'html': markup, 'url': reference.get('url', '')}


def propose_picks(proposal_id, payload, actor_id):
    proposal = db.session.get(SourceProposal, proposal_id)
    if proposal is None or not isinstance(payload, dict):
        raise ValueError('来源建议不存在或点选格式无效')
    _soup, nodes, reference = _document(proposal)
    if payload.get('evidence_hash') != reference['hash']:
        raise ValueError('网页证据已更新，请重新打开快照')
    picks = payload.get('picks', {})
    if not isinstance(picks, dict) or set(picks) - {'row', 'title', 'link', 'date'}:
        raise ValueError('点选字段无效')
    row = nodes.get(str(picks.get('row')))
    title = nodes.get(str(picks.get('title')))
    if row is None or title is None or row.name in ('html', 'body', 'head'):
        raise ValueError('请点选一条完整通知及其中的标题')
    config = json.loads(proposal.candidate_json)
    config['list_selector'] = _path(row.parent) + ' > ' + row.name
    for field, pick in [('title_selector', 'title'), ('link_selector', 'link'), ('date_selector', 'date')]:
        element = nodes.get(str(picks.get(pick)))
        if pick == 'link' and element is None:
            element = title if title.name == 'a' else title.find('a')
        if element is None:
            if pick == 'date':
                config[field] = ''
                continue
            raise ValueError('请点选通知的原文链接')
        if element is not row and not any(parent is row for parent in element.parents):
            raise ValueError('标题、链接和日期必须来自同一条通知')
        config[field] = _path(element, row)
    result = queue_source_review(proposal.school_id, config, department_id=proposal.department_id,
                                requested_by=actor_id)
    return result
