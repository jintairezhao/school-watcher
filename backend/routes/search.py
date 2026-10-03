from backend.services.summaries import current_summaries, summary_expression
"""搜索路由：全局 / 学校内关键词搜索通知 + 关键词高亮过滤器"""
import re
import html as _html
from collections import OrderedDict

from flask import Blueprint, render_template, request, g
from markupsafe import Markup
from sqlalchemy import and_, or_

from backend.database.db import db
from backend.database.models import School, Announcement

bp = Blueprint('search', __name__)


def highlight(text, query):
    """将文本中的关键词高亮（先 HTML 转义，再用 <mark> 包裹命中的关键词）。"""
    if not text:
        return Markup('')
    escaped = _html.escape(str(text))
    keywords = [k for k in (query or '').split() if k.strip()]
    if not keywords:
        return Markup(escaped)
    for kw in keywords:
        esc_kw = _html.escape(kw)
        if not esc_kw:
            continue
        pattern = re.compile(re.escape(esc_kw), re.IGNORECASE)
        escaped = pattern.sub(lambda m: f'<mark>{m.group(0)}</mark>', escaped)
    return Markup(escaped)


def snippet(text, query, max_len=160):
    """提取正文中首个命中关键词的上下文片段并高亮；正文不含关键词时返回空。"""
    if not text:
        return Markup('')
    text = str(text).strip()
    keywords = [k for k in (query or '').split() if k.strip()]
    if not keywords:
        return Markup('')
    lower = text.lower()
    pos, kw_len = -1, 0
    for kw in keywords:
        p = lower.find(kw.lower())
        if p != -1 and (pos == -1 or p < pos):
            pos, kw_len = p, len(kw)
    if pos == -1:
        return Markup('')
    start = max(0, pos - 40)
    end = min(len(text), pos + kw_len + 80)
    seg = text[start:end]
    if start > 0:
        seg = '…' + seg
    if end < len(text):
        seg = seg + '…'
    return highlight(seg, query)


# 注册模板过滤器（blueprint 注册时自动应用到 app）
bp.add_app_template_filter(highlight, 'highlight')
bp.add_app_template_filter(snippet, 'snippet')


@bp.route('/search')
def search():
    """关键词搜索通知。可选 school_id 限定在单所学校内搜索。"""
    q = request.args.get('q', '').strip()
    school_id = request.args.get('school_id', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = 50

    results = []
    total = 0
    pagination = None
    school = None
    grouped_by_school = OrderedDict()
    display_sources = {}
    announcement_sources = {}

    if q:
        from backend.services.announcement_sources import source_expression, sources_for, preferred_source
        query = Announcement.query.filter(source_expression())
        if school_id:
            school = db.session.get(School, school_id)
            query = query.filter(source_expression(school_ids=[school_id]))

        # 空格分隔多关键词，词与词之间 AND，标题 / 摘要 / 正文任一命中即可
        keywords = [k for k in q.split() if k.strip()]
        word_conditions = []
        for kw in keywords:
            like = f'%{kw}%'
            word_conditions.append(or_(
                Announcement.title.ilike(like),
                Announcement.content_text.ilike(like),
                summary_expression().ilike(like),
            ))
        if word_conditions:
            query = query.filter(and_(*word_conditions))

        pagination = (
            query.order_by(
                Announcement.published_at.desc().nullslast(),
                Announcement.created_at.desc(),
            )
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        results = pagination.items
        total = pagination.total
        announcement_sources = sources_for([a.id for a in results])
        from backend.services.source_channels import channels_for
        channels_for({source.id: source for rows in announcement_sources.values() for source in rows}.values())
        display_sources = {a.id: preferred_source(a, announcement_sources.get(a.id, []), school_id)
                           for a in results}

        # 全局搜索按学校分组展示
        if results and not school_id:
            for ann in results:
                grouped_by_school.setdefault(display_sources[ann.id].school, []).append(ann)

    from backend.services import read_state
    read_ids = (read_state.read_ids_for(g.user.id, [a.id for a in results])
                if g.get('user') else set())

    return render_template(
        'search.html',
        q=q,
        school=school,
        school_id=school_id,
        results=results,
        summary_texts=current_summaries(results),
        total=total,
        pagination=pagination,
        grouped_by_school=grouped_by_school,
        read_ids=read_ids,
        display_sources=display_sources,
        announcement_sources=announcement_sources,
    )
