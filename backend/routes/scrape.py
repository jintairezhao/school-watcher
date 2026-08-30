"""爬取与摘要 API"""
import json
import logging
import threading

from flask import Blueprint, request, jsonify, Response, current_app

from backend.database.db import db
from backend.database.models import School
from backend.auth import admin_required

logger = logging.getLogger(__name__)

bp = Blueprint('scrape', __name__)


@bp.route('/api/scrape/<int:school_id>', methods=['POST'])
@admin_required
def api_trigger_scrape(school_id):
    """手动触发爬取（后台执行 + SSE 进度推送）"""
    school = db.session.get(School, school_id)
    if not school:
        return jsonify({'success': False, 'error': '学校不存在'}), 404

    from backend.scraper.engine import scrape_school
    from backend.scraper.progress.scrape_progress import create_session as create_scrape_session
    from backend.ai.summarizer import batch_summarize

    session = create_scrape_session(school_id, school.name)
    app = current_app._get_current_object()

    def _run():
        app.app_context().push()
        try:
            # 在后台线程中重新获取 school，避免 detached instance 问题
            school_obj = db.session.get(School, school_id)
            log = scrape_school(school_obj, progress_session=session)
            if log.new_count > 0:
                # 摘要阶段可能耗时数分钟，发事件让前端按钮显示状态，避免「74/74 空转」
                session.emit({'type': 'summarizing',
                              'message': f'抓取完成，正在为 {log.new_count} 条新通知生成 AI 摘要…'})
                # 摘要生成失败不应把整次爬取标记为失败，单独兜底
                try:
                    batch_summarize()
                except Exception as e:
                    logger.error(f"摘要生成失败 [school {school_id}]: {e}")
            session.complete(
                new_count=log.new_count,
                message=f'爬取完成，新增 {log.new_count} 条通知'
            )
        except Exception as e:
            logger.error(f"手动爬取失败 [school {school_id}]: {e}")
            session.fail(str(e))

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({
        'success': True,
        'session_id': session.session_id,
        'message': '抓取已启动',
    })


@bp.route('/api/scrape/<int:school_id>/events')
@admin_required
def api_scrape_events(school_id):
    """SSE 端点：实时推送抓取进度"""
    from backend.scraper.progress.scrape_progress import get_school_session as get_scrape_session

    session = get_scrape_session(school_id)
    if not session:
        def no_session():
            yield f"data: {json.dumps({'type': 'error', 'message': '没有活跃的抓取会话'}, ensure_ascii=False)}\n\n"
        return Response(no_session(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    return Response(
        session.events_generator(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@bp.route('/api/scrape/<int:school_id>/status')
@admin_required
def api_scrape_status(school_id):
    """获取抓取会话状态（SSE 轮询回退）"""
    from backend.scraper.progress.scrape_progress import get_school_session as get_scrape_session

    session = get_scrape_session(school_id)
    if not session:
        return jsonify({'status': 'none', 'message': '没有活跃的抓取会话'})

    return jsonify(session.to_dict())


@bp.route('/api/scrape/all', methods=['POST'])
@admin_required
def api_trigger_scrape_all():
    """手动触发全部爬取（后台执行 + SSE 进度推送）"""
    from backend.scraper.engine import scrape_school, active_schools_query
    from backend.scraper.progress.scrape_progress import create_session as create_scrape_session
    from backend.ai.summarizer import batch_summarize

    schools = active_schools_query().all()
    if not schools:
        return jsonify({'success': False, 'error': '没有启用且有订阅的学校'}), 400

    # 为每所学校创建会话（前端按 school_id 查询）
    sessions = {}
    for school in schools:
        sessions[school.id] = create_scrape_session(school.id, school.name)

    app = current_app._get_current_object()

    def _run():
        app.app_context().push()
        total_all_new = 0
        for school in schools:
            sess = sessions[school.id]
            try:
                # 在后台线程中重新获取 school，避免 detached instance 问题
                school_obj = db.session.get(School, school.id)
                log = scrape_school(school_obj, progress_session=sess)
                total_all_new += log.new_count
                sess.complete(
                    new_count=log.new_count,
                    message=f'{school.name} 爬取完成，新增 {log.new_count} 条'
                )
            except Exception as e:
                logger.error(f"全量爬取失败 [{school.name}]: {e}")
                sess.fail(str(e))

        if total_all_new > 0:
            for sess in sessions.values():
                sess.emit({'type': 'summarizing',
                           'message': f'抓取完成，正在为 {total_all_new} 条新通知生成 AI 摘要…'})
            try:
                batch_summarize()
            except Exception as e:
                logger.error(f"全量爬取摘要生成失败: {e}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({
        'success': True,
        'session_ids': {school_id: sess.session_id for school_id, sess in sessions.items()},
        'message': f'全量抓取已启动（{len(schools)} 所学校）',
    })


@bp.route('/api/summarize', methods=['POST'])
@admin_required
def api_trigger_summarize():
    """触发批量摘要生成"""
    from backend.ai.summarizer import batch_summarize

    data = request.get_json() or {}
    ids = data.get('ids', None)
    try:
        count = batch_summarize(ids)
        return jsonify({'success': True, 'count': count, 'message': f'已为 {count} 条通知生成摘要'})
    except Exception as e:
        logger.error(f"摘要生成失败: {e}")
        return jsonify({'success': False, 'error': '摘要生成失败，请确认 API Key 已正确配置'}), 500
