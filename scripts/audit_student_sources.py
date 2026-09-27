"""Inventory student-source leads across every school; never estimate coverage from crawl counts."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.source_inventory import Inventory, DEFAULT_PATH
from backend.scraper.http_client import same_school_url

ACADEMIC = re.compile(r'院系设置|学院设置|院系介绍|院系专业|学院与专业|教学机构|教学单位|教学科研机构|教学科研单位|教学和科研单位|教学科研内设机构|学部与院系|学部设置|^(院系|二级院系|二级学院|教学学院|学部学院|教学院)$')
MAJORS = re.compile(r'本科专业|专业设置|专业介绍|专业目录|专业一览')
TEACHING = re.compile(r'教务|本科生院|本科教学')
ARTICLE_CONTEXT = re.compile(r'关于|负责人|候选|考察|人选|征求|通知|公示|大会|会议')
SERVICE_LABEL = re.compile(r'(?:系统|平台|登录|登陆|认证)(?:入口|导航)?$')
SERVICE_PATH = re.compile(r'/(?:sso|tyrz|caslogin|authserver|login)(?:[./]|$)', re.I)
CATEGORIES = ('academic_directories', 'major_directories', 'teaching_entrances')


def classify_lead(page):
    label = re.sub(r'\s+', '', page['label'])
    if len(label) > 30 or page['state'] == 'reference_only':
        return []
    if SERVICE_LABEL.search(label) or SERVICE_PATH.search(urlsplit(page.get('final_url') or page.get('url', '')).path):
        return []
    return [category for category, pattern in zip(CATEGORIES, (ACADEMIC, MAJORS, TEACHING))
            if pattern.search(label) and (category == 'teaching_entrances' or not ARTICLE_CONTEXT.search(label))]


def audit(inventory):
    # Read one consistent metadata snapshot while crawlers keep their own progress.
    with inventory.connect() as connection:
        connection.execute('BEGIN')
        sites = [dict(r) for r in connection.execute('SELECT * FROM sites ORDER BY name,root_url')]
        pages = [dict(r) for r in connection.execute(
            "SELECT site_key,url,final_url,label,kind,state,status_code,checked_at,health,content_hash "
            "FROM pages WHERE kind='root' OR length(label)<=45")]
        aliases = [dict(r) for r in connection.execute("""
            SELECT e.site_key,e.target_url AS url,p.final_url,e.label,p.kind,
                   coalesce(p.state,'not_queued') AS state,p.status_code,p.checked_at,p.health,
                   e.parent_url AS reference_url
            FROM edges e JOIN pages origin ON origin.site_key=e.site_key AND origin.url=e.parent_url
                AND origin.state='fetched' AND origin.content_hash=e.content_hash
            LEFT JOIN pages p ON p.site_key=e.site_key AND p.url=e.target_url
            WHERE e.target_url<>'' AND length(e.label)<=30
              AND e.decision IN ('follow','official_external_link','external_review')
              AND (e.label LIKE '%院%' OR e.label LIKE '%专业%' OR e.label LIKE '%教务%'
                   OR e.label LIKE '%教学%' OR e.label LIKE '%学部%')
        """)]
        counts = defaultdict(Counter)
        for row in connection.execute('SELECT site_key,state,count(*) AS amount FROM pages GROUP BY site_key,state'):
            counts[row['site_key']][row['state']] = row['amount']
        reviews = [dict(r) for r in connection.execute('SELECT site_key,baseline_json,result_json FROM reference_checks')]
    by_site = defaultdict(list)
    for page in pages + aliases:
        by_site[page['site_key']].append(page)
    review_scopes = defaultdict(list)
    for row in reviews:
        baseline, result = json.loads(row['baseline_json']), json.loads(row['result_json'])
        review_scopes[row['site_key']].append({
            'id': baseline['id'], 'category': baseline['category'], 'scope': baseline['scope'],
            'reference_url': baseline['reference_url'], 'reviewed_at': baseline['reviewed_at'],
            'saved_result': result['status'],
            'note': '局部历史核对记录；本清单不重新验证内容，也不把它视为全校验收。'})
    schools = []
    for site in sites:
        key = site['site_key']
        root = next((p for p in by_site[key] if p['url'] == site['root_url']), None)
        local = defaultdict(list)
        external = defaultdict(list)
        for page in by_site[key]:
            for category in classify_lead(page):
                target = page.get('final_url') or page['url']
                destination = local if same_school_url(target, site['root_url']) else external
                existing = next((p for p in destination[category] if p['url'] == page['url']), None)
                if existing is None:
                    existing = {k: page[k] for k in (
                        'url', 'final_url', 'label', 'kind', 'state', 'status_code', 'checked_at', 'health')}
                    existing.update(observed_labels=[], reference_urls=[])
                    destination[category].append(existing)
                if page['label'] not in existing['observed_labels']:
                    existing['observed_labels'].append(page['label'])
                if page.get('reference_url') and page['reference_url'] not in existing['reference_urls']:
                    existing['reference_urls'].append(page['reference_url'])
        categories = {}
        for category in CATEGORIES:
            leads = local[category]
            fetched = [p for p in leads if p['state'] == 'fetched']
            status = ('not_located' if not leads else 'fetched_needs_review' if fetched else 'located_not_read')
            categories[category] = {'status': status, 'lead_count': len(leads), 'fetched_count': len(fetched),
                                    'state_counts': dict(Counter(p['state'] for p in leads)),
                                    'leads': sorted(leads, key=lambda p: (p['state'] != 'fetched', p['label'], p['url'])),
                                    'external_leads_require_owner_review': external[category]}
        schools.append({'site_key': key, 'name': site['name'], 'root_url': site['root_url'],
                        'root_state': root['state'] if root else 'missing',
                        'root_status_code': root['status_code'] if root else None,
                        'historical_crawl_counts': dict(counts[key]), 'student_sources': categories,
                        'recorded_local_reviews': review_scopes[key],
                        'student_coverage_percent': None, 'student_scope_accepted': False})
    return {'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'school_count': len(schools),
            'method': '按已发现入口的明确标签整理候选，保留原访问状态；域外入口另列待归属核对。',
            'limitations': ['没有找到候选入口不代表官网不存在该目录。',
                            '候选入口、抓取页面和文章数量均不作为覆盖率分母。',
                            '专业招生目录不自动等于当前开设专业；教务相关链接不自动等于通知来源。',
                            '已有局部核对记录不代表全校院系、专业或通知来源完整。'],
            'summary': {category: dict(Counter(s['student_sources'][category]['status'] for s in schools))
                        for category in CATEGORIES}, 'schools': schools}


def write_markdown(result, path):
    labels = dict(zip(CATEGORIES, ('院系目录', '专业目录', '教务入口')))
    statuses = {'not_located': '未定位', 'located_not_read': '已知待读取', 'fetched_needs_review': '已读取候选，待核对'}
    lines = ['# 学生相关来源核对清单', '',
             f"范围：{result['school_count']} 所学校。生成时间：{result['generated_at']}。", '',
             '本清单整理已发现入口，不是来源覆盖率报告。已读取只表示取得页面，可能仍为空目录、招生介绍或官网错误页；没有定位入口也不代表官网不存在。', '',
             '同址导航别名一并核对；标题涉及院系或专业的普通新闻不作为目录。学校主页直接列出的正式入口仍需独立读取，不能用某个学院的局部目录代替。', '',
             '| 范围 | 未定位明确入口 | 入口已知，尚未读到 | 已读取候选，待核对 |',
             '|---|---:|---:|---:|']
    for category in CATEGORIES:
        counts = result['summary'][category]
        lines.append(f"| {labels[category]} | {counts.get('not_located', 0)} | {counts.get('located_not_read', 0)} | {counts.get('fetched_needs_review', 0)} |")
    lines += ['', '全量学生相关来源的 90% 匹配尚未验收；上表均为学校数量，不能当作匹配率。', '',
              '| 学校 | 院系目录 | 专业目录 | 教务入口 |', '|---|---|---|---|']
    for school in result['schools']:
        cells = []
        for category in CATEGORIES:
            scope = school['student_sources'][category]
            cells.append(f"{statuses[scope['status']]}（{scope['fetched_count']}/{scope['lead_count']} 个候选已读）")
        lines.append(f"| [{school['name']}]({school['root_url']}) | " + ' | '.join(cells) + ' |')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, default=DEFAULT_PATH)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/source-audits/student-source-coverage.json')
    parser.add_argument('--markdown', type=Path)
    args = parser.parse_args()
    result = audit(Inventory(args.inventory))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.markdown:
        write_markdown(result, args.markdown)
    print(json.dumps({'school_count': result['school_count'], 'summary': result['summary']}, ensure_ascii=True))
