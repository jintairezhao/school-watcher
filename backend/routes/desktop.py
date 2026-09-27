"""Status of optional local components; native-session protection runs first."""
import os
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

bp = Blueprint('desktop', __name__)


@bp.route('/_desktop/components', methods=['GET', 'POST'])
def components():
    if not current_app.config.get('DESKTOP_MODE'):
        abort(404)
    from desktop.browser import read_status
    data = Path(os.environ['WATCHER_DATA_DIR'])
    if request.method == 'POST':
        (data / 'desktop-browser-retry.request').touch()
        return jsonify(phase='checking', message='正在重新检测采集组件…')
    return jsonify(read_status(data))
