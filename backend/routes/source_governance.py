"""Read coverage and handle evidence-backed source proposals without developer edits."""
import json
from flask import Blueprint, jsonify, request, g, render_template
from backend.auth import admin_required, login_required
from backend.database.db import db
from backend.database.models import School
from backend.database.source_governance_models import SourceProposal
from backend.services import source_governance as governance

bp = Blueprint('source_governance', __name__)


@bp.route('/admin/sources')
@admin_required
def review_page():
    return render_template('source_review.html')


@bp.route('/api/schools/<int:school_id>/onboarding')
@login_required
def school_status(school_id):
    db.get_or_404(School, school_id)
    return jsonify(governance.school_governance_status(school_id))


@bp.route('/api/admin/schools/<int:school_id>/onboarding/baselines', methods=['POST'])
@admin_required
def register_baselines(school_id):
    db.get_or_404(School, school_id)
    payload = request.get_json(silent=True)
    if (not isinstance(payload, dict) or not isinstance(payload.get('baselines'), list)
            or not 1 <= len(payload['baselines']) <= 50 or request.content_length and request.content_length > 1024 * 1024):
        return jsonify(error='请提供不超过 1MB 的独立核实机构名录'), 400
    from backend.services.tasks import enqueue
    from hashlib import sha256
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    task = enqueue('source_baseline', f'{school_id}:{digest}', {'school_id': school_id,
        'baselines': payload['baselines'], 'complete_roster': payload.get('complete_roster') is True,
        'actor_id': g.user.id}, capability='directory', replace_finished=False)
    return jsonify(task_id=task.id, message='已加入官方名录核对队列'), 202


@bp.route('/api/admin/schools/<int:school_id>/onboarding/scopes/<unit_key>', methods=['PUT'])
@admin_required
def review_scope(school_id, unit_key):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error='核实结论格式不正确'), 400
    try:
        result = governance.review_unit_scope(school_id, unit_key, payload.get('state'), g.user.id,
            evidence_reference=payload.get('evidence_reference', {}),
            priority_scopes=payload.get('priority_scopes'), note=payload.get('note', ''))
        return jsonify(result)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        db.session.rollback()
        return jsonify(error=str(exc)), 400


@bp.route('/api/admin/source-proposals')
@admin_required
def proposals():
    query = SourceProposal.query
    school_id = request.args.get('school_id', type=int)
    if school_id:
        query = query.filter_by(school_id=school_id)
    state = request.args.get('state', 'needs_review')
    if state != 'all':
        query = query.filter_by(state=state)
    page = max(1, request.args.get('page', 1, type=int))
    records = query.order_by(SourceProposal.updated_at.desc()).paginate(page=page, per_page=30, error_out=False)
    school_names = {s.id: s.name for s in School.query.filter(School.id.in_({r.school_id for r in records.items}))}
    return jsonify(items=[dict(governance.serialize_proposal(p), school_name=school_names.get(p.school_id, ''))
                          for p in records.items], total=records.total, page=page, pages=records.pages)


@bp.route('/api/admin/source-proposals/<int:proposal_id>')
@admin_required
def proposal_detail(proposal_id):
    proposal = db.get_or_404(SourceProposal, proposal_id)
    data = governance.serialize_proposal(proposal)
    bundle = json.loads(proposal.evidence_json or '{}')
    # Serve evidence as text via JSON. Never execute a captured school's scripts.
    from bs4 import BeautifulSoup
    snapshots = []
    for reference in [bundle.get('list'), bundle.get('independent'), *bundle.get('articles', []),
                      *bundle.get('identity_snapshots', [])]:
        if not reference:
            continue
        try:
            soup = BeautifulSoup(governance.read_snapshot(reference), 'lxml')
            for element in soup(['script', 'style', 'noscript']):
                element.decompose()
            snapshots.append({'url': reference.get('url'), 'role': reference.get('role'),
                              'hash': reference.get('hash'), 'text': soup.get_text(' ', strip=True)[:8000]})
        except (ValueError, OSError):
            snapshots.append({'url': reference.get('url'), 'error': '证据缓存已过期，请重新检查'})
    return jsonify(dict(data, evidence=snapshots))


@bp.route('/api/admin/source-proposals/<int:proposal_id>/review', methods=['POST'])
@admin_required
def review(proposal_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error='操作格式不正确'), 400
    try:
        action = payload.get('action')
        result = governance.review_proposal(proposal_id, action, g.user.id, payload.get('note', ''))
        if action != 'reject':
            from backend.services.tasks import enqueue
            task = enqueue('source_review', proposal_id, {'proposal_id': proposal_id}, capability='directory')
            result['task_id'] = task.id
        return jsonify(result), 200 if action == 'reject' else 202
    except ValueError as exc:
        db.session.rollback()
        return jsonify(error=str(exc)), 400


@bp.route('/api/admin/source-proposals/<int:proposal_id>/picker', methods=['GET', 'POST'])
@admin_required
def picker(proposal_id):
    from backend.services.source_picker import preview, propose_picks
    try:
        result = (preview(proposal_id) if request.method == 'GET' else
                  propose_picks(proposal_id, request.get_json(silent=True), g.user.id))
        return jsonify(result), 200 if request.method == 'GET' else 202
    except (ValueError, OSError) as exc:
        db.session.rollback()
        return jsonify(error=str(exc)), 400


@bp.route('/api/admin/departments/<int:department_id>/versions')
@admin_required
def versions(department_id):
    from backend.database.source_governance_models import SourceConfigVersion
    rows = SourceConfigVersion.query.filter_by(department_id=department_id).order_by(SourceConfigVersion.version.desc()).all()
    return jsonify([{'version': row.version, 'config': json.loads(row.config_json),
                     'created_at': row.created_at.isoformat()} for row in rows])


@bp.route('/api/admin/departments/<int:department_id>/rollback', methods=['POST'])
@admin_required
def rollback(department_id):
    from backend.database.source_governance_models import SourceConfigVersion
    from backend.database.models import Department
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or type(payload.get('version')) is not int:
        return jsonify(error='请选择需要恢复的配置版本'), 400
    row = SourceConfigVersion.query.filter_by(department_id=department_id, version=payload['version']).first()
    department = db.session.get(Department, department_id)
    if not row or not department:
        return jsonify(error='配置版本不存在'), 404
    try:
        return jsonify(governance.queue_source_review(department.school_id, json.loads(row.config_json),
                       department_id=department.id, requested_by=g.user.id)), 202
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
