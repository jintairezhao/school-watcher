"""Nonblocking article content requests. URLs are always taken from stored notices."""
from flask import Blueprint, Response, jsonify, request, g, send_file
from requests import RequestException
from backend.database.models import Announcement
from backend.services.announcement_sources import source_expression
from backend.services.content_cache import status, request_content
from backend.services.article_images import render_article_html, image_sources, fetch_article_image

bp = Blueprint('content', __name__)


@bp.route('/api/announcements/<int:ann_id>/student-information', methods=['GET', 'POST'])
def student_information(ann_id):
    if not g.get('user'):
        return jsonify(error='请登录后使用分析'), 401
    ann = Announcement.query.filter(Announcement.id == ann_id, source_expression()).first()
    if ann is None:
        return jsonify(error='通知不存在或来源已下架'), 404
    from backend.services.student_information import article_view, material, queue_assessment
    from backend.database.models import BackgroundTask
    insight = article_view(ann)
    if insight:
        from flask import render_template_string
        html = render_template_string("{% from '_student_insight.html' import student_insight %}"
                                      "{{ student_insight(ann, insight, user) }}", ann=ann, insight=insight, user=g.user)
        return jsonify(state='ready', active=False, html=html)
    prepared = material('article', ann_id)
    if request.method == 'POST':
        from backend.auth.rate_limit import check_rate_limit
        if not check_rate_limit(f'student-analysis:user:{g.user.id}', 60, 3600)[0]:
            return jsonify(error='分析请求较频繁，请稍后重试'), 429
        if not prepared:
            return jsonify(error='请在正文加载完成后再点击分析'), 409
        from backend.ai.configuration import get_model_binding, AIConfigError
        try:
            get_model_binding('directory')
        except AIConfigError:
            return jsonify(error='请先在设置中配置 AI；普通阅读不受影响'), 409
        queue_assessment('article', ann_id, requested_by=g.user.id)
    job = BackgroundTask.query.filter(BackgroundTask.kind == 'student_assessment',
        BackgroundTask.payload['subject_kind'].as_string() == 'article',
        BackgroundTask.payload['subject_id'].as_integer() == ann_id,
        BackgroundTask.payload['input_hash'].as_string() == (prepared[2] if prepared else '')
        ).order_by(BackgroundTask.updated_at.desc()).first()
    active = bool(job and job.state in ('pending', 'running', 'waiting') and job.payload.get('requested_by') is not None)
    message = '正在分析正文，完成后会自动显示' if active else '尚无分析结果，可按需点击分析'
    if job and not active:
        message = '本次分析未完成，可稍后重试；通知正文仍可正常阅读'
        if (job.result or {}).get('error_code') == 'budget_exhausted':
            message = 'AI 用量已达上限，通知正文仍可正常阅读'
        if (job.result or {}).get('state') == 'stale':
            message = '正文已更新，可按需重新分析'
    return jsonify(state=job.state if job else 'idle', active=active, message=message), 202 if active else 200


@bp.route('/api/announcements/<int:ann_id>/content', methods=['GET', 'POST'])
def article_content(ann_id):
    from backend.services.saved_materials import readable_expression
    ann = Announcement.query.filter(Announcement.id == ann_id, readable_expression(g.user.id if g.get('user') else None)).first()
    if not ann:
        return jsonify(error='通知不存在或来源已下架'), 404
    if request.method == 'POST':
        from backend.auth.rate_limit import check_rate_limit
        key = f'body:user:{g.user.id}' if g.get('user') else f'body:ip:{request.remote_addr}'
        if not check_rate_limit(key, 120, 3600)[0]:
            return jsonify(error='正文读取较频繁，请稍后重试'), 429
        request_content(ann)
    if request.method == 'GET' and (ann.content_cached_at or ann.content_html):
        request_content(ann)
    state = status(ann)
    from backend.services.publication_dates import publication_date_label, publication_date_stamp
    from backend.services.content_cache import progress_message
    return jsonify(content_status=state, error=ann.content_error if state == 'failed' else '',
                   progress_message=progress_message(ann) if state=='loading' else '',
                   published_at=publication_date_stamp(ann.published_at),
                   publication_date_label=publication_date_label(ann.published_at),
                   content_html=render_article_html(ann.content_html, ann.id, ann.url) if state == 'saved' else '',
                   content_text=ann.content_text if state == 'saved' else ''), (202 if state == 'loading' else 200)


@bp.get('/api/announcements/<int:ann_id>/images/<string:image_key>')
def article_image(ann_id, image_key):
    from backend.services.saved_materials import readable_expression, resource_url, resource_key
    ann = Announcement.query.filter(Announcement.id == ann_id, readable_expression(g.user.id if g.get('user') else None)).first()
    if not ann:
        return jsonify(error='通知不存在或来源已下架'), 404
    url = image_sources(ann.content_html, ann.url).get(image_key)
    if not url:
        return jsonify(error='通知中没有这张图片'), 404
    from backend.database.db import db
    from backend.database.models import AnnouncementResource
    from sqlalchemy.orm import undefer
    resource = AnnouncementResource.query.options(undefer(AnnouncementResource.payload)).filter_by(
        announcement_id=ann_id, url_key=resource_key(resource_url(url))).first()
    # One observed URL can be both an inline image and a downloadable attachment.
    # The validated image bytes determine rendering, not which tag came first.
    if resource and resource.state == 'ready':
        from backend.services.article_images import _image_type
        try:
            mime = _image_type(resource.payload or b'')
        except ValueError:
            return jsonify(error='本地图片尚未保存完整，请重新保存收藏'), 503
        return Response(resource.payload, mimetype=mime, headers={'Cache-Control': 'private, max-age=3600'})
    article_url = ann.url
    from backend.database.db import db
    db.session.commit()  # Release the read transaction before the outbound request.
    try:
        data, mime = fetch_article_image(url, article_url)
    except (RequestException, ValueError):
        return jsonify(error='图片暂时无法读取，请稍后重试或打开官网原文'), 502
    return Response(data, mimetype=mime, headers={'Cache-Control': 'private, max-age=3600'})


@bp.get('/api/announcements/<int:ann_id>/resources/<string:key>')
def saved_resource(ann_id, key):
    from backend.services.saved_materials import readable_expression
    from backend.database.db import db
    from backend.database.models import AnnouncementResource
    import hashlib
    import io
    ann = Announcement.query.filter(Announcement.id == ann_id,
        readable_expression(g.user.id if g.get('user') else None)).first()
    from sqlalchemy.orm import undefer
    resource = AnnouncementResource.query.options(undefer(AnnouncementResource.payload)).filter_by(
        announcement_id=ann_id,url_key=key).first() if ann else None
    if not resource or resource.state != 'ready' or resource.payload is None:
        return jsonify(error='附件尚未保存完成'), 404
    if resource.sha256 and hashlib.sha256(resource.payload).hexdigest() != resource.sha256:
        return jsonify(error='本地附件校验未通过，请重新保存收藏'), 503
    response = send_file(io.BytesIO(resource.payload), mimetype='application/octet-stream',
        as_attachment=True, download_name=resource.filename or '官网附件',
        etag=resource.sha256 or None, max_age=0)
    response.headers['Cache-Control'] = 'private, no-cache'
    return response
