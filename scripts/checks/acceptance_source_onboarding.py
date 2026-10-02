"""受控副本上的来源接入验收：真实官网页面 → 取证 → 验证 → 接入 → 采集。

安装版数据目录与开发库都不在写入范围内。所有写入都落在 --data-dir 指向的目录副本，
因此这个脚本可以对着真实大学官网运行，而不会碰到用户正在使用的数据库。

默认不调用模型（只有显式给 --allow-ai 才会），所以这一步证明的是
「不依赖 AI 也能把真实公开栏目接入」；模型路径被调用时，它实际收到的材料会被记录下来，
用来判断「模型要某页 → 程序去取」这条回路是否真的存在。
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def configure_environment(data_dir, discovery_cache):
    """Point every data path at the verification copy before the app is created."""
    os.environ['WATCHER_DATA_DIR'] = str(data_dir)
    os.environ['WATCHER_DISCOVERY_CACHE_PATH'] = str(discovery_cache)
    # Seeding imports config/schools.yaml during create_app. A verification copy
    # must not silently gain rows it did not inherit from the install.
    os.environ['WATCHER_SEED_ON_START'] = '0'
    os.environ.setdefault('WATCHER_BROWSER', '0')


class SkillRecorder:
    """Stands in for the model, and records what the model would have been given."""

    def __init__(self):
        self.calls = []

    def __call__(self, skill, phase, evidence, capability, execution_id, **kwargs):
        references = evidence.get('evidence') or []
        self.calls.append({
            'skill': skill, 'phase': phase, 'execution_id': execution_id,
            'candidate_count': len(evidence.get('candidates') or []),
            'entities': [e.get('name') for e in evidence.get('entities') or []],
            'observed_url_count': len(evidence.get('observed_urls') or []),
            'evidence_ids': [r.get('evidence_id') for r in references],
            # A body page is readable by the model only if some reference carries
            # DOM beyond the list excerpt; this is what "ask for more material"
            # would have to be answered from.
            'reference_shapes': sorted({tuple(sorted(r.keys())) for r in references}),
        })
        return {'status': 'stubbed_by_acceptance_harness', 'output': {}}


def evidence_shape(bundle):
    if not bundle:
        return {'empty': True}
    samples = bundle.get('samples') or {}
    return {
        'keys': sorted(bundle.keys()),
        'articles': len(bundle.get('articles') or []),
        'has_pagination': bool(bundle.get('pagination')),
        'has_independent': bool(bundle.get('independent')),
        'article_samples': sum(1 for k, v in samples.items() if v == 'obtained' and k.startswith('article')),
        'sample_outcomes': samples,
        'region': bundle.get('region') or {},
        'config_name': (bundle.get('config') or {}).get('name'),
        'preflight_errors': bundle.get('preflight_errors'),
    }


def snapshot_proposal(pid):
    from backend.database.db import db
    from backend.database.models import Department
    from backend.database.source_governance_models import SourceProposal
    proposal = db.session.get(SourceProposal, pid)
    if not proposal:
        return {'id': pid, 'missing': True}
    department = db.session.get(Department, proposal.department_id) if proposal.department_id else None
    return {
        'id': pid,
        'state': proposal.state,
        'department_id': proposal.department_id,
        'department_name': department.name if department else None,
        'candidate': json.loads(proposal.candidate_json),
        'validation': json.loads(proposal.validation_json or '{}'),
        'evidence': evidence_shape(json.loads(proposal.evidence_json or '')),
    }


def departments_for(config):
    from backend.services.source_inventory import canonical_url
    from backend.database.models import Department
    url = canonical_url(config.get('list_url') or '')
    return [d for d in Department.query.all() if canonical_url(d.list_url or '') == url]


def collect_announcements(department_id):
    from backend.database.models import Announcement
    return Announcement.query.filter_by(department_id=department_id).count()


def run(args):
    from backend import create_app
    from backend.database.db import db

    app = create_app()
    report = {'data_dir': str(args.data_dir), 'allow_ai': args.allow_ai, 'proposals': []}

    recorder = SkillRecorder()
    if not args.allow_ai:
        import backend.ai.runtime as ai_runtime
        ai_runtime.run_skill = recorder

    with app.app_context():
        from backend.services.source_governance import process_source_review

        for pid in args.proposals:
            record = {'proposal_id': pid, 'before': snapshot_proposal(pid)}
            if record['before'].get('missing'):
                report['proposals'].append(record)
                continue
            skill_calls_before = len(recorder.calls)
            try:
                outcome = process_source_review({'proposal_id': pid})
                record['outcome'] = outcome
                record['error'] = None
            except Exception as exc:  # A crash is a finding, not a reason to stop.
                db.session.rollback()
                record['outcome'] = None
                record['error'] = f'{type(exc).__name__}: {exc}'[:400]
            record['after'] = snapshot_proposal(pid)
            # Only the department this proposal is bound to counts as installed by
            # it. A shared address (two candidates on one homepage) must not make a
            # neighbour's department look like this candidate's own activation.
            bound = record['after'].get('department_id')
            near = [{'id': d.id, 'name': d.name, 'list_url': d.list_url,
                     'list_selector': d.list_selector, 'school_id': d.school_id}
                    for d in departments_for(record['after']['candidate'])]
            record['departments'] = [d for d in near if d['id'] == bound]
            record['same_address_departments'] = [d['id'] for d in near if d['id'] != bound]
            record['model_calls'] = recorder.calls[skill_calls_before:]
            report['proposals'].append(record)

        if args.collect:
            report['collection'] = []
            for record in report['proposals']:
                for department in record['departments']:
                    before = collect_announcements(department['id'])
                    try:
                        from backend.services.source_collection import collect_source
                        from backend.database.models import Department
                        result = collect_source(db.session.get(Department, department['id']))
                        error = None
                    except Exception as exc:
                        db.session.rollback()
                        result, error = None, f'{type(exc).__name__}: {exc}'[:400]
                    after = collect_announcements(department['id'])
                    report['collection'].append({
                        'proposal_id': record['proposal_id'], 'department': department['name'],
                        'department_id': department['id'],
                        'announcements_before': before, 'announcements_after': after,
                        'collected': after - before, 'result': result, 'error': error,
                    })

    return report


def summarize(report):
    lines = []
    for record in report['proposals']:
        after = record.get('after') or {}
        before = record['before']
        lines.append('=' * 72)
        lines.append(f"候选 {record['proposal_id']}: {before['candidate'].get('name')!r} "
                     f"{before['candidate'].get('list_url')}")
        lines.append(f"  状态 {before['state']} -> {after.get('state')}")
        lines.append(f"  证据 前: {json.dumps(before['evidence'], ensure_ascii=False)[:200]}")
        ev = json.dumps(after.get('evidence'), ensure_ascii=False)
        lines.append(f"  证据 后: {ev[:300]}")
        lines.append(f"  验证错误: {after.get('validation', {}).get('errors')}")
        if record.get('error'):
            lines.append(f"  异常: {record['error']}")
        if record['departments']:
            for d in record['departments']:
                lines.append(f"  本候选接入的部门: #{d['id']} {d['name']} selector={d['list_selector'][:60]!r}")
        else:
            lines.append("  本候选接入的部门: 无")
        if record.get('same_address_departments'):
            lines.append(f"  （同地址已存在的其他部门，非本候选接入: {record['same_address_departments']}）")
        lines.append(f"  未读到的条目: {record['after'].get('validation', {}).get('limitations')}")
        for call in record.get('model_calls') or []:
            lines.append(f"  模型调用: {json.dumps(call, ensure_ascii=False)[:300]}")
    for item in report.get('collection') or []:
        lines.append(f"  采集 {item['department']}: {item['announcements_before']} -> "
                     f"{item['announcements_after']} (+{item['collected']}) "
                     f"result={json.dumps(item['result'], ensure_ascii=False)[:160]} err={item['error']}")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True,
                        help='受控副本目录（安装版数据目录的副本），所有写入都发生在这里')
    parser.add_argument('--discovery-cache', default='',
                        help='副本内的 discovery_cache.sqlite3；省略则用副本目录下的同名文件')
    parser.add_argument('--proposals', default='1,3,4,5,7', help='要复跑的真实候选编号，逗号分隔')
    parser.add_argument('--allow-ai', action='store_true',
                        help='允许真实模型调用（默认不调用，只记录模型本会收到的材料）')
    parser.add_argument('--no-collect', dest='collect', action='store_false', default=True,
                        help='跳过接入后的采集验证')
    parser.add_argument('--out', default='', help='把结构化报告写到这个 JSON 文件')
    args = parser.parse_args()

    args.data_dir = Path(args.data_dir).resolve()
    args.discovery_cache = Path(args.discovery_cache).resolve() if args.discovery_cache \
        else args.data_dir / 'discovery_cache.sqlite3'
    if not (args.data_dir / 'school_watcher.db').is_file():
        parser.error(f'{args.data_dir} 里没有 school_watcher.db；请先复制安装版数据目录')
    args.proposals = [int(p) for p in str(args.proposals).split(',') if p.strip()]

    configure_environment(args.data_dir, args.discovery_cache)
    report = run(args)
    print(summarize(report))
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                                  encoding='utf-8')
        print(f'\n结构化报告: {args.out}')


if __name__ == '__main__':
    main()
