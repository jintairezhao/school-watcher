"""Explicit source lifecycle actions; existing hard-delete routes stay separate."""
import logging
from flask import Blueprint, jsonify, request
from sqlalchemy.exc import SQLAlchemyError
from backend.auth import admin_required
from backend.database.db import db
from backend.database.models import Department
from backend.services.source_lifecycle import set_source_state, states_for
from backend.services.structure_versions import current_version

bp = Blueprint('source_lifecycle', __name__)
logger = logging.getLogger(__name__)


@bp.post('/api/departments/<int:source_id>/lifecycle')
@admin_required
def update_source_lifecycle(source_id):
    source = db.session.get(Department, source_id)
    if source is None:
        return jsonify(error='来源不存在'), 404
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get('state') not in ('active', 'disabled', 'removed'):
        return jsonify(error='请选择有效的信息来源状态'), 400
    school_id = source.school_id
    try:
        resume_collection = False
        if data['state'] == 'active' and source.school.is_effectively_active():
            # Resolve catalog-backed subscription expansion before writing. It
            # may read other stores and must not enter our mutation transaction.
            from backend.services.directory_options import expand_directory_ids
            from backend.database.models import Subscription
            subs = Subscription.query.filter_by(school_id=school_id).all()
            resume_collection = bool(source.list_url) and any(sub.department_ids is None or source.id in
                expand_directory_ids(school_id, sub.department_ids) for sub in subs)
        changed = set_source_state(source, data['state'])
        db.session.flush()
        if changed and resume_collection:
            from backend.services.tasks import enqueue
            enqueue('collect', source.id, {'school_id': school_id, 'department_id': source.id}, commit=False)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        return jsonify(error=str(exc)), 400
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception('Source lifecycle update failed: %s', source_id)
        return jsonify(error='来源状态未修改，历史信息仍保留。请稍后重试。'), 500
    return jsonify(success=True, source_id=source_id, state=states_for([source_id])[source_id],
                   changed=changed, structure_version=current_version(school_id))
