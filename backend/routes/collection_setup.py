"""Review shared collection settings before any first subscription side effect."""
from datetime import date
from flask import Blueprint, g, request, jsonify, render_template, abort
from backend.auth import login_required
from backend.database.db import db
from backend.database.models import AppConfig, School
from backend.ai.configuration import get_model_binding, AIConfigError, KEY_PAGES
from backend.ai.providers import PROVIDER_NAMES
from backend.services.collection_settings import since_date, validate_settings, save_settings

bp = Blueprint('collection_setup', __name__)


@bp.route('/collection/setup')
@login_required
def page():
    target = {'kind': 'settings', 'name': ''}
    school_id = request.args.get('school_id', type=int)
    if school_id:
        school = db.get_or_404(School, school_id)
        if not school.enabled:
            abort(404)
        target = {'kind': 'school', 'school_id': school.id, 'name': school.name}
    elif request.args.get('add') == '1':
        target = {'kind': 'add', 'name': request.args.get('name', '')[:200],
                  'url': request.args.get('url', '')[:500]}
    elif request.args.get('name'):
        from backend.services.catalog import find_entry
        entry = find_entry(request.args['name'])
        if not entry:
            abort(404)
        target = {'kind': 'catalog', 'name': entry['name']}
    try:
        binding = get_model_binding('directory')
    except AIConfigError:
        binding = None
    response = render_template('collection_setup.html', target=target, ai_ready=bool(binding),
        use_ai=AppConfig.get('collection_ai_mode', 'ai' if binding else 'rules') == 'ai',
        ai_label=(binding['name'] + ' · ' + binding['model']) if binding else '',
        provider_guides=[{'name':PROVIDER_NAMES[key], 'url':url} for key, url in KEY_PAGES.items()],
        since_date=since_date().strftime('%Y-%m-%d'), today=date.today().isoformat(),
        interval=AppConfig.get('scrape_interval', '30'), collection_setup=True)
    from flask import make_response
    response = make_response(response)
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.route('/api/collection/setup', methods=['POST'])
@login_required
def save():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get('ai_mode') not in ('ai', 'rules'):
        return jsonify(error='请选择探索方式'), 400
    if payload['ai_mode'] == 'ai':
        try:
            get_model_binding('directory')
        except AIConfigError:
            return jsonify(error='请先测试并选择学校来源识别服务，或选择暂不使用 AI'), 400
    if g.user.is_admin:
        if not {'since_date', 'interval'} <= payload.keys():
            return jsonify(error='请设置起始发布日期和更新间隔'), 400
        try:
            cutoff, interval = validate_settings(payload)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        save_settings(cutoff, interval, complete=True, ai_mode=payload['ai_mode'])
    else:
        # Shared server readers cannot silently change every subscriber's settings.
        if {'since_date', 'interval'} & payload.keys():
            return jsonify(error='共享抓取设置需要管理员修改'), 403
        from backend.services.collection_settings import setup_required
        if setup_required():
            return jsonify(error='请管理员先完成本实例的基础配置'), 403
    return jsonify(success=True, ai_assist=payload['ai_mode'] == 'ai')
