"""User-facing responsibility is separate from candidate and validation state."""
import json
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from backend.database.db import db
from backend.database.models import BackgroundTask
from backend.database.source_governance_models import SourceReviewEvent
from backend.services.source_inventory import canonical_url

LABELS = {
    'article_sample_missing': '程序尚未取得足够的通知正文样本',
    'independent_sample_missing': '程序尚未完成栏目独立复核',
    'publisher_requires_review': 'AI 尚未找到足够的官方发布部门依据',
    'publisher_conflict': '官网材料中的发布部门存在冲突',
    'column_identity_or_scope_unconfirmed': '尚未确认栏目名称与列表范围一致',
    'column_region_requires_review': '同页栏目范围尚未确定',
    'invalid_list_item': '提取结果中混入了非通知条目',
    'pagination_sample_missing': '程序尚未完成下一页检查',
    'invalid_publication_date': '日期格式尚未正确解析',
    'article_identity_mismatch': '通知正文与列表标题不一致',
    'source_login_required': '官网要求登录后才能读取',
}


def workflow(proposal):
    validation = json.loads(proposal.validation_json)
    saved = validation.get('workflow', {})
    errors = validation.get('errors', [])
    task = BackgroundTask.query.filter_by(identity=f'source_review:{proposal.id}').first()
    active = task and task.state in ('pending', 'running', 'waiting_browser', 'waiting_resource')
    raw = ' '.join(map(str, errors)) + ' ' + (task.error if task else '')
    status = saved.get('status', 'recognition_incomplete')
    if task and task.state == 'failed' and task.error:
        status = 'program_error'
    if 'Unknown acquisition purpose' in raw or 'No such file or directory' in raw:
        status = 'program_error'
    elif 'source_login_required' in raw or saved.get('error_code') == 'source_login_required':
        status = 'login_required'
    elif saved.get('outcome') in ('denied', 'needs_manual'):
        status = 'access_limited'
    elif saved.get('outcome') in ('network_error', 'unavailable'):
        status = 'site_unavailable'
    if proposal.state == 'activated':
        status = 'activated'
    elif proposal.state == 'rejected':
        status = 'rejected'
    elif proposal.state == 'superseded':
        status = 'superseded'
    elif proposal.state == 'not_applicable':
        status = 'not_a_column'
    elif active:
        status = 'checking'
    elif proposal.state == 'proposed' and status != 'program_error':
        status = 'checking' if active else 'recognition_incomplete'
    states = {
        'checking': ('正在自动检查或补充材料', 'program', '程序会继续读取官网、交给 AI 判断并验证栏目。'),
        'recognition_incomplete': ('自动识别未完成', 'ai', '本轮自动尝试已结束，材料与进度已保存，等待程序适配；不影响其他栏目和已有通知。'),
        'login_required': ('官网要求登录', 'website', '仅采集公开内容；此页面暂不接入。可在官网自行查看。'),
        'access_limited': ('官网限制访问', 'website', '访问限制解除后可重新检查；AI 无法解除官网限制。'),
        'site_unavailable': ('官网暂时无法访问', 'program', '程序会进行有限次数的重试；已有通知保留。'),
        'program_error': ('程序执行失败', 'developer', '需要开发者修复；修复版本会对这条记录重新检查。'),
        'user_choice': ('需要你选择', 'user', '请选择下方具体对象，程序会继续验证。'),
        'activated': ('已正式接入', 'program', '后续采集复用已保存配置。'),
        'superseded': ('已整理到所属栏目', 'program', '请查看下方对应栏目。'),
        'not_a_column': ('不是发布栏目', 'ai', '这是机构介绍、人物目录等资料页，保留判断依据；程序继续探索其他发布入口。'),
        'rejected': ('已拒绝', 'user', '保留你的选择，自动恢复不会重新接入。'),
    }
    label, owner, next_step = states.get(status, states['recognition_incomplete'])
    if status == 'site_unavailable' and SourceReviewEvent.query.filter_by(proposal_id=proposal.id, action='access_retry').count() >= 2:
        next_step = '本轮重试已结束；官网恢复后可重新检查，已有通知保留。'
    reason = saved.get('reason') or '；'.join(LABELS.get(x, x) for x in errors)
    if status == 'program_error' and not reason and task:
        reason = f'后台检查任务异常结束，错误已保存在任务 {task.id} 的记录中'
    return {**saved, 'status': status, 'label': label, 'owner': owner,
            'requires_user': status == 'user_choice', 'user_action': '请选择希望订阅的栏目' if status == 'user_choice' else '你现在无需操作',
            'next_step': next_step, 'reason': reason, 'needed_evidence': saved.get('needed_evidence', []),
            'stages': {'ai': saved.get('ai_status', 'not_needed'),
                       'validation': 'passed' if validation.get('passed') else 'not_passed',
                       'activation': 'activated' if proposal.state == 'activated' else 'not_activated'}}


def supported_question(proposal, question, evidence):
    """Only choices about observed objects; no open-ended request for user evidence."""
    known = {ref['evidence_id']: ref for ref in evidence['evidence']}
    options = question.get('options', [])
    if not 2 <= len(options) <= 6 or not question.get('text') or question.get('purpose') != 'subscription_preference':
        return None
    accepted = []
    for option in options:
        refs = option.get('evidence_ids', [])
        if not refs or any(x not in known for x in refs):
            return None
        field, value = option.get('field'), option.get('value', '')
        if field == 'list_url':
            if value not in evidence.get('observed_urls', []):
                return None
            if not any(any(canonical_url(urljoin(known[ref].get('url', ''), a.get('href', ''))) == canonical_url(value)
                           for a in BeautifulSoup(known[ref].get('html', ''), 'lxml').select('a[href]')) for ref in refs):
                return None
        else:
            return None
        accepted.append({**option, 'evidence_urls': [known[r].get('url') for r in refs if known[r].get('url')]})
    if len({x.get('id') for x in accepted}) != len(accepted):
        return None
    return {'text': question['text'], 'options': accepted}


def select_option(proposal, choice, actor_id):
    from backend.services import source_governance as g
    saved = json.loads(proposal.validation_json).get('workflow', {})
    option = next((x for x in saved.get('question', {}).get('options', []) if x['id'] == choice), None)
    if saved.get('status') != 'user_choice' or not option:
        raise ValueError('这个选择已失效，请刷新后查看当前问题')
    config = json.loads(proposal.candidate_json)
    config[option['field']] = option['value']
    if option['field'] == 'list_url':
        config.update(name=option['label'], list_selector='', title_selector='', link_selector='', date_selector='')
    proposal.candidate_json = g._json(config)
    proposal.revision += 1; proposal.validated_hash = None; proposal.state = 'proposed'
    db.session.add(SourceReviewEvent(proposal_id=proposal.id, actor_id=actor_id, action='select_observed_option',
                                   detail_json=g._json(option)))
    db.session.commit()


def school_publisher_proven(bundle, config):
    """School-level lists: an observed main-site route plus target identity.

    A school is already the supplied trust root, not a department that must
    appear in its own organizational directory. Subdomains do not inherit this.
    """
    from urllib.parse import urlsplit
    from backend.services.source_governance import read_snapshot, _normal
    root, target = bundle.get('root_url', ''), config.get('list_url', '')
    school = bundle.get('school_name', '')
    if not school or config.get('group_name') not in ('', school):
        return False
    if urlsplit(target).netloc != urlsplit(root).netloc:
        return False
    refs = list((bundle.get('publisher_material') or {}).values()) + bundle.get('identity_snapshots', [])
    homes = [ref for ref in refs if canonical_url(ref['url']) == canonical_url(root)]
    linked = any(any(canonical_url(urljoin(root, a['href'])) == canonical_url(target)
                     for a in BeautifulSoup(read_snapshot(ref), 'lxml').select('a[href]')) for ref in homes)
    if not linked or not bundle.get('list'):
        return False
    page = BeautifulSoup(read_snapshot(bundle['list']), 'lxml')
    return any(_normal(school) in _normal(node.get_text(' ', strip=True) or node.get('alt', ''))
               for node in page.select('title,h1,header,footer,img[alt]'))


def publisher_proven(bundle, config):
    """Verify AI-cited official directory → branded home → column, on captured DOMs."""
    from backend.services.source_governance import read_snapshot, _normal, _inside
    from backend.scraper.http_client import same_school_url
    proof, refs = bundle.get('publisher_evidence', {}), bundle.get('publisher_material', {})
    name = config.get('group_name', '')
    if not name or proof.get('name') != name:
        return False
    directory = refs.get(proof.get('directory_evidence_id'))
    home = refs.get(proof.get('homepage_evidence_id'))
    if not directory or not home:
        return False
    root = bundle.get('root_url', '')
    if not all(same_school_url(ref['url'], root) for ref in (directory, home)):
        return False
    # Directory must be reachable by links from the official root in captured material.
    reachable = {canonical_url(root)}
    for _ in range(4):
        for ref in refs.values():
            if canonical_url(ref['url']) in reachable:
                soup = BeautifulSoup(read_snapshot(ref), 'lxml')
                reachable.update(canonical_url(urljoin(ref['url'], a['href'])) for a in soup.select('a[href]'))
    if canonical_url(directory['url']) not in reachable:
        return False
    soup = BeautifulSoup(read_snapshot(directory), 'lxml')
    official = any(canonical_url(urljoin(directory['url'], a['href'])) == canonical_url(home['url'])
        and _normal(a.get_text(' ', strip=True)) == _normal(name) for a in soup.select('a[href]'))
    branding = BeautifulSoup(read_snapshot(home), 'lxml')
    branded = any(_normal(name) in _normal(n.get_text(' ', strip=True) or n.get('alt', ''))
                  for n in branding.select('title,h1,header,footer,img[alt]'))
    linked = canonical_url(config['list_url']) == canonical_url(home['url']) or any(
        canonical_url(urljoin(home['url'], a['href'])) == canonical_url(config['list_url']) for a in branding.select('a[href]'))
    # Related links to another department are not proof that this unit publishes
    # its notices. A separate site also needs the target's own publisher identity.
    target_branded = _inside(config['list_url'], home['url'])
    target = bundle.get('list')
    if not target_branded and target and canonical_url(target['url']) == canonical_url(config['list_url']):
        page = BeautifulSoup(read_snapshot(target), 'lxml')
        target_branded = any(_normal(name) in _normal(n.get_text(' ', strip=True) or n.get('alt', ''))
                             for n in page.select('title,h1,header,footer,img[alt]'))
    return official and branded and linked and target_branded and same_school_url(config['list_url'], root)
