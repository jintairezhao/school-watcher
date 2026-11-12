"""Read-only official structure and source evidence, available beside subscriptions."""
import json
from flask import Blueprint, render_template, request, abort

from backend.database.db import db
from backend.database.models import School
from backend.services.source_inventory import site_key
from backend.services.structure_facts import _url

bp = Blueprint('source_structure', __name__)
RELATIONS = {
    'sub_unit': '目录列示，隶属关系待核实', 'attached_unit': '目录列示，隶属关系待核实',
    'unit_channel': '栏目入口，归属待核实',
    'directory_entry': '目录列示', 'directory_entry_no_link': '官网未提供链接',
    'nested_directory_entry': '目录嵌套', 'menu_group': '官网导航分组',
    'navigation_entry': '导航入口', 'linked_unit_unverified': '隶属关系待核实',
    'page_identity': '页面入口',
    'major_directory_entry': '专业目录条目',
    'programme_college': '专业目录列示学院',
    'programme_joint_group': '官网联合培养分组，不代表独立学院',
    'shared_table_row': '官网同列多个单位，具体挂靠范围待核实',
    'same_table_row': '官网同一表格行', 'table_reference': '表格备注入口',
    'non_entity_directory_entry': '官网标注：非实体学院',
    'linked_navigation_unverified': '链接入口，栏目关系待核实',
    'campus_group': '官网按校区分组，不代表行政隶属',
    'campus_directory_entry': '在该校区列示',
    'publication_column': '栏目入口，归属待核实',
    'publication_group': '官网栏目分组',
    'footer_link': '页脚链接，栏目归属待核实',
    'academic_group': '官网教学科研分组，不代表行政隶属',
    'shared_directory_row': '官网同一条目并列，不代表上下级',
    'same_directory_row': '官网同一条目并列',
    'directory_companion_entry': '官网附加列示，隶属关系待核实',
    'hidden_directory_entry': '官网模板标为隐藏，关系待核实',
    'shared_directory_label': '官网同一入口列出多个名称',
    'directory_group': '官网机构分类，不代表行政隶属',
    'unit_profile_entry': '单位介绍入口，栏目归属待核实',
    'shared_site_navigation': '网站公共导航，发布单位待核实',
    'profile_reference': '介绍页参考链接，发布单位待核实',
    'directory_label_pending': '目录名称被截断，待核实',
}
HEALTH = {'unverified': '尚未检查', 'unreachable': '暂时无法访问', 'date_unknown': '发布时间待核实',
          'stale': '超过两年未见更新', 'infrequent': '超过一年未见更新',
          'recent_publication': '有近期发布', 'retirement_notice': '页面含停用或迁移提示',
          'dynamic_content': '动态页面，具体内容待核实'}


def page_health_label(page):
    if page.get('ownership_pending'):
        return '正在查找官网归属依据'
    if 'official_template_error_requires_review' in (page.get('notes_json') or ''):
        return '部分页面内容需要继续适配'
    if 'directory_variant_differences:' in (page.get('notes_json') or ''):
        return '官网目录不同版本存在差异'
    if 'official_directory_link_requires_review:' in (page.get('notes_json') or ''):
        return '正在寻找可用的替代入口'
    if page.get('state') == 'fetched' and page.get('kind') == 'major':
        return '专业入口已读取'
    if page.get('state') == 'fetched' and 'unit_profile_page:' in (page.get('notes_json') or ''):
        return '介绍正文待更新' if 'unit_profile_content_pending' in page['notes_json'] else '单位介绍页，发布栏目待核实'
    return HEALTH.get(page.get('health', 'unverified'), '待核实')


def evidence_tree(records, pages):
    # A successful HTTP response also observes its final address. A queued copy
    # of that address must not hide the visit, nor may an older redirect erase
    # a newer failure. Client-side app-shell routes do not establish this alias.
    observed_pages = dict(pages)
    for page in pages.values():
        final = page.get('final_url')
        checked = page.get('checked_at')
        if not final or not checked or page.get('state') != 'fetched' or page.get('health') == 'dynamic_content':
            continue
        current = observed_pages.get(final)
        if current is None or not current.get('checked_at') or checked > current['checked_at']:
            observed_pages[final] = page
    by_key = {}
    placements = {}
    aliases = {}
    evidence = {}
    for record in records:
        by_key.setdefault(record['node_key'], record)
        placement = (record['node_key'], record['parent_key'])
        placements.setdefault(placement, record)
        reference = _url(record.get('reference_url'))
        proof = {field: record.get(field) for field in ('locator', 'content_hash', 'observed_at', 'material_available')}
        proof['reference_url'] = reference
        page = pages.get(reference, {})
        proof['current_material'] = bool(reference and proof['locator'] and proof['content_hash']
            and proof['observed_at'] and page.get('state') == 'fetched'
            and page.get('content_hash') == proof['content_hash'])
        if proof not in evidence.setdefault(placement, []):
            evidence[placement].append(proof)
        if address := _url(record.get('url')):
            aliases.setdefault(record['node_key'], set()).add(address)
    children, roots = {}, []
    for record in placements.values():
        if record['parent_key'] and record['parent_key'] in by_key and record['parent_key'] != record['node_key']:
            children.setdefault(record['parent_key'], []).append(record)
        else:
            roots.append(record)
    def expand(record, ancestors):
        key = record['node_key']
        page = observed_pages.get(record['url'], {})
        node = dict(record, relation_label=RELATIONS.get(record['relation'], '待核实'),
                    health_label=page_health_label(page),
                    urls=sorted(aliases.get(key, []), reverse=True), children=[], cycle=key in ancestors,
                    evidence=evidence[(key, record['parent_key'])])
        if record['relation'] == 'non_entity_directory_entry' and record['name'].startswith('（非独立科研平台）'):
            node['relation_label'] = '官网标注：非独立科研平台'
        if key not in ancestors:
            node['children'] = [expand(child, ancestors | {key}) for child in children.get(key, [])]
        return node
    return [expand(record, set()) for record in roots]


def proven_page_relations(facts, page):
    """Display claims only from the shared facts with this page's current proof.

    Raw tree placement remains a parser observation even when a separately
    verified organization relation uses the same row. Its enum is not evidence.
    """
    nodes = {node['id']: node for node in facts.get('nodes', [])}
    claims, entrances = [], []
    reference = _url(page.get('url'))
    for relation in facts.get('relations', []):
        source, target = nodes.get(relation['source']), nodes.get(relation['target'])
        if not source or not target:
            continue
        proofs = [proof for proof in relation.get('evidence', [])
                  if proof.get('kind') == 'web_observation' and _url(proof.get('reference_url'))]
        item = dict(relation, source_node=source, target_node=target, evidence=proofs)
        if (relation['type'] == 'navigation_link' and target['entity_type'] == 'page'
                and source['entity_type'] == 'page' and _url(target.get('url')) == reference and proofs):
            entrances.append(item)
        if not any(_url(proof['reference_url']) == reference and proof.get('content_hash') == page.get('content_hash')
                   and proof.get('locator') and proof.get('observed_at') for proof in proofs):
            continue
        if relation['type'] == 'organization_membership':
            labels = {'sub_unit': '下属单位', 'attached_unit': '挂靠单位'}
            if relation.get('observed_relation') not in labels or not relation.get('assertions'):
                continue
            item['relation_label'] = labels[relation['observed_relation']]
        elif relation['type'] == 'source_ownership' and relation.get('assertions'):
            item['relation_label'] = '栏目归属'
        else:
            continue
        claims.append(item)
    return claims, entrances


def major_directory_tree(records, pages):
    identities = {n['node_key'] for n in records if n['relation'] == 'page_identity'}
    # A navigation link to this page can have the same identity as the page
    # itself. It is another placement, not an ancestor of the page's majors.
    candidates = [n for n in records if n['relation'] == 'page_identity' or n['node_key'] not in identities]
    wanted = {n['node_key'] for n in candidates if n['kind'] == 'major'}
    while True:
        parents = {n['parent_key'] for n in candidates if n['node_key'] in wanted and n['parent_key']}
        if parents <= wanted:
            break
        wanted.update(parents)
    return evidence_tree([n for n in candidates if n['node_key'] in wanted], pages)


@bp.route('/schools/<int:school_id>/structure')
def school_structure(school_id):
    school = db.get_or_404(School, school_id)
    if not school.enabled:
        abort(404)
    from backend.services.structure_snapshot import read_structure
    try:
        capture = read_structure(school, include_catalog_report=True)
    except RuntimeError:
        abort(503)
    key = site_key(school.url)
    report = capture['catalog_report']
    tree, references, selected, issues = [], [], None, []
    variants = []
    directory_link_issues = []
    proven_relations, page_entrances = [], []
    major_tree = []
    programme_contexts, programme_attributes = [], {}
    if report:
        # This page is public. Do not emit credential-bearing URL identities or
        # rewrite them into another page that could be mistaken for the source.
        report['pages'] = [dict(page, url=_url(page.get('url')), final_url=_url(page.get('final_url')))
                           for page in report.get('pages', []) if _url(page.get('url'))]
        report.setdefault('states', {})
        report['reference_checks'] = [check for check in report.get('reference_checks', []) if _url(check.get('reference_url'))]
        pages = {p['url']: p for p in report['pages']}
        from backend.services.source_ownership import PREFIX, domain_evidence
        ownership_pages = [p for p in report['pages'] if PREFIX in (p.get('notes_json') or '')]
        for page in pages.values():
            page['ownership_pending'] = ('external_ownership_requires_review' in (page['notes_json'] or '')
                and not domain_evidence(ownership_pages, key, page['final_url'] or page['url']))
        references = [p for p in report['pages'] if p['state'] == 'fetched' and
                      (p['kind'] in ('root', 'directory', 'unit', 'major') or
                       'reviewed_major_directory' in (p.get('notes_json') or '') or
                       (p['kind'] == 'channel' and p.get('feed_json')))]
        # Open a school-level roster before deep unit pages named "机构设置".
        references.sort(key=lambda p: (p['kind'] != 'directory', p['depth'],
            not any(label in p['label'] for label in ('院系', '学部与院系', '学院设置', '教学单位')),
            p['label'], p['url']))
        reference_url = request.args.get('reference')
        selected = next((p for p in references if p['url'] == reference_url), None)
        if not selected:
            selected = references[0] if references else None
        records = capture['raw_structure_nodes']
        if selected:
            page_records = [n for n in records if _url(n.get('reference_url')) == selected['url']]
            tree = evidence_tree(page_records, pages)
            proven_relations, page_entrances = proven_page_relations(capture['facts'], selected)
            if 'reviewed_major_directory' in (selected.get('notes_json') or ''):
                # Keep the original ancestors, but show the programme directory
                # before the unrelated public navigation repeated on this page.
                major_tree = major_directory_tree(page_records, pages)
                tree = evidence_tree([n for n in page_records if n['kind'] != 'major'], pages)
            for note in json.loads(selected['notes_json']):
                if note.startswith('directory_variant_differences:'):
                    variants = json.loads(note.split(':', 1)[1])
                    for entry in variants:
                        for version in entry.get('versions', []):
                            version['urls'] = [_url(address) for address in version.get('urls', [])]
                if note.startswith('official_directory_link_requires_review:'):
                    directory_link_issues.append(json.loads(note.split(':', 1)[1]))
                if note.startswith('programme_catalog_context:'):
                    programme_contexts.append(json.loads(note.split(':', 1)[1]))
                if note.startswith('programme_catalog_attributes:'):
                    programme_attributes.update({entry['key']: entry['fields'] for entry in json.loads(note.split(':', 1)[1])})
        # An old publication date, variant directory or isolated CMS widget
        # error is not a failed source. A readable list continues collecting.
        def readable_region(page):
            try:
                feed = json.loads(page.get('feed_json') or '{}')
                return any(column.get('name') and column.get('container_locator') and column.get('samples')
                           for column in feed.get('lists', [feed]))
            except (TypeError, ValueError):
                return False
        issues = [dict(p, health_label=page_health_label(p)) for p in report['pages']
                  if p['state'] in ('failed', 'blocked') or p['health'] in ('retirement_notice', 'dynamic_content')
                  or p.get('ownership_pending') or ('official_template_error_requires_review' in (p.get('notes_json') or '')
                  and not readable_region(p))]
    issue_total = len(issues)
    issue_pages = max(1, (issue_total + 49) // 50)
    issue_page = min(issue_pages, max(1, request.args.get('issues_page', 1, type=int)))
    issues = issues[(issue_page - 1) * 50:issue_page * 50]
    return render_template('source_structure.html', school=school, report=report, tree=tree,
                           references=references, selected=selected, issues=issues,
                           issue_total=issue_total, issue_page=issue_page, issue_pages=issue_pages,
                           directory_variants=variants, proven_relations=proven_relations,
                           page_entrances=page_entrances, major_tree=major_tree, structure_version=capture['version'],
                           directory_link_issues=directory_link_issues, programme_contexts=programme_contexts,
                           programme_attributes=programme_attributes)
