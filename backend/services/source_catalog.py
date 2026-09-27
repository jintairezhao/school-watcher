"""Map observed publication pages to subscriptions without merging unrelated names."""
import json

from backend.database.db import db
from backend.database.models import Department
from backend.scraper.http_client import validate_public_url, same_school_url
from backend.services.source_inventory import canonical_url

FIELDS = ('list_url', 'list_selector', 'title_selector', 'link_selector', 'date_selector',
          'content_selector', 'group_name')


def publication_candidates(report, structure=(), focus='all'):
    if focus not in ('all', 'student'):
        raise ValueError('Unknown discovery focus')
    from backend.scraper.discovery.structure import health_from_evidence
    from backend.services.source_ownership import PREFIX
    from backend.services.source_relationships import SourceRelationships
    relationships = SourceRelationships(report, structure)
    ownership_pages = [p for p in report['pages'] if PREFIX in (p.get('notes_json') or '')]
    result = []
    for page in report['pages']:
        if not page['feed_json'] or page['state'] != 'fetched':
            continue
        root_url = report.get('site', {}).get('root_url')
        owner_evidence = None
        if not root_url:
            continue
        if not same_school_url(page['final_url'] or page['url'], root_url):
            from backend.services.source_ownership import domain_evidence
            owner_evidence = domain_evidence(ownership_pages, report['site']['site_key'],
                                            page['final_url'] or page['url'])
            if not owner_evidence:
                continue
        evidence = json.loads(page['feed_json'])
        path = json.loads(page['path_json'])
        source_paths = relationships.paths_for(page['final_url'] or page['url'])
        feeds = evidence.get('lists', [evidence])
        for feed in feeds:
            feed_paths = relationships.paths_for(feed.get('column_url')) if feed.get('column_url') else []
            feed_paths = feed_paths or source_paths
            feed_roster_owners = relationships.publication_owners(page['final_url'] or page['url'], feed_paths)
            feed_owners = feed_roster_owners or {p['unit_name'] for p in feed_paths
                                                if p.get('basis') == 'school_website_entry'}
            feed_group = next(iter(feed_owners)) if len(feed_owners) == 1 else ''
            heading_pending = not feed.get('name') or bool(feed.get('heading_ambiguous'))
            name = '栏目名称待核实' if heading_pending else feed['name']
            student_rank = None
            if focus == 'student':
                from .student_sources import student_priority, TEACHING, NON_STUDENT
                context = json.dumps(path + sorted(feed_owners), ensure_ascii=False)
                student_rank = student_priority('channel', feed.get('name') or page['label'],
                                                context, report['site']['name'])
                # A mixed central list may also carry teaching notices. Keep it
                # as a candidate when an observed article supplies that lead.
                if student_rank is None and not NON_STUDENT.search(feed.get('name') or page['label']):
                    if any(TEACHING.search(s.get('title', '')) for s in feed.get('samples', [])):
                        student_rank = 4
                if student_rank is None:
                    continue
            health = health_from_evidence('', feed.get('latest_publication'))
            if health in ('stale', 'infrequent') and not feed.get('publication_dates_complete'):
                health = 'date_unknown'
            result.append({
                'name': name, 'group_name': feed_group[:200],
                'list_url': page['final_url'] or page['url'],
                **{field: feed.get(field, '') for field in FIELDS if field not in ('list_url', 'group_name')},
                'confidence': feed.get('confidence', 0), 'item_count': feed.get('item_count', 0),
                'sample_titles': [s['title'] for s in feed.get('samples', [])[:3]],
                'source_health': ('retirement_notice' if page['health'] == 'retirement_notice' else
                                  health),
                'source_verified': False,
                'source_heading_pending': heading_pending,
                'source_page_label': page['label'],
                'source_owner_evidence': owner_evidence,
                'source_structure': feed_paths,
                'discovery_path': path,
                'heading_locator': feed.get('heading_locator', ''),
                'container_locator': feed.get('container_locator', ''),
                'column_url': feed.get('column_url', ''),
                'column_group_name': feed.get('column_group_name', ''),
                **({'discovery_priority': student_rank} if focus == 'student' else {}),
            })
    return sorted(result, key=lambda c: c['discovery_priority']) if focus == 'student' else result


def apply_source_configs(school_id, configs):
    """Compatibility entrypoint: untrusted configurations become review proposals.

    Deliberately does not trust ``source_verified`` or install raw selectors. A
    worker must capture independent evidence and use activate_proposal instead.
    """
    if not isinstance(configs, list) or not configs:
        raise ValueError('请提供要接入的来源列表')
    validated = []
    for cfg in configs:
        if not isinstance(cfg, dict) or not isinstance(cfg.get('name'), str) or not cfg['name'].strip():
            raise ValueError('来源名称不能为空')
        if len(cfg['name']) > 200 or any(not isinstance(cfg.get(f, ''), str) for f in FIELDS):
            raise ValueError('来源字段格式不正确')
        validate_public_url(cfg.get('list_url', ''), resolve=False)
        validated.append(cfg)
    from backend.services.source_governance import propose_source, source_config
    existing = Department.query.filter_by(school_id=school_id).all()
    for cfg in validated:
        url = canonical_url(cfg['list_url'])
        selector = cfg.get('list_selector', '')
        matches = [d for d in existing if canonical_url(d.list_url or '') == url
                   and d.name == cfg['name'].strip()]
        dept = next((d for d in matches if (d.list_selector or '') == selector), None)
        candidate = {field: cfg.get(field, '') for field in ('name', *FIELDS)}
        if dept and source_config(dept) == candidate:
            continue
        propose_source(school_id, candidate, department_id=dept.id if dept else None, origin='configuration')
    return 0
