"""Nonblocking article content requests. URLs are always taken from stored notices."""
from flask import Blueprint, Response, jsonify, request, g
from requests import RequestException
from backend.database.models import Announcement
from backend.services.announcement_sources import source_expression
from backend.services.content_cache import status, request_content
from backend.services.article_images import render_article_html, image_sources, fetch_article_image

bp = Blueprint('content', __name__)


@bp.route('/api/announcements/<int:ann_id>/content', methods=['GET', 'POST'])
def article_content(ann_id):
    ann = Announcement.query.filter(Announcement.id == ann_id, source_expression()).first()
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
    return jsonify(content_status=state, error=ann.content_error if state == 'failed' else '',
                   content_html=render_article_html(ann.content_html, ann.id, ann.url) if state == 'saved' else '',
                   content_text=ann.content_text if state == 'saved' else ''), (202 if state == 'loading' else 200)


@bp.get('/api/announcements/<int:ann_id>/images/<string:image_key>')
def article_image(ann_id, image_key):
    ann = Announcement.query.filter(Announcement.id == ann_id, source_expression()).first()
    if not ann:
        return jsonify(error='通知不存在或来源已下架'), 404
    url = image_sources(ann.content_html, ann.url).get(image_key)
    if not url:
        return jsonify(error='通知中没有这张图片'), 404
    article_url = ann.url
    from backend.database.db import db
    db.session.commit()  # Release the read transaction before the outbound request.
    try:
        data, mime = fetch_article_image(url, article_url)
    except (RequestException, ValueError):
        return jsonify(error='图片暂时无法读取，请稍后重试或打开官网原文'), 502
    return Response(data, mimetype=mime, headers={'Cache-Control': 'private, max-age=3600'})
