"""Application-wide locale infrastructure, separate from school content and identity."""
from functools import lru_cache
import json
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import g, has_request_context, request

LOCALES = ('zh-CN', 'en')
DEFAULTS = {'locale':'zh-CN', 'time_zone':None, 'education_system':'cn'}
PREFIX = 'localization:user:'


@lru_cache(maxsize=1)
def english_catalog():
    return json.loads((Path(__file__).resolve().parents[2] / 'frontend/static/i18n/en.json').read_text('utf8'))


def decode_preferences(raw):
    try:
        value = json.loads(raw or '{}')
        return validate_preferences(value) if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def validate_preferences(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('本土化设置格式无效')
    result = dict(value)
    if 'locale' in value and value['locale'] not in LOCALES:
        raise ValueError('请选择中文或 English')
    if 'education_system' in value and value['education_system'] not in ('cn','global'):
        raise ValueError('请选择有效的教育体系')
    if 'time_zone' in value:
        zone = value['time_zone']
        if zone in (None, ''):
            result['time_zone'] = None
        else:
            if not isinstance(zone,str) or len(zone)>80:
                raise ValueError('请填写有效的 IANA 时区名称')
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('请填写有效的 IANA 时区名称') from None
    return result


def preferences(user_id=None):
    from backend.database.models import AppConfig
    if user_id is None and has_request_context():
        user_id = getattr(g.get('user'), 'id', None)
    saved = decode_preferences(AppConfig.get(PREFIX + str(user_id))) if user_id is not None else {}
    result = dict(DEFAULTS, **saved)
    if 'locale' not in saved and has_request_context() and request.cookies.get('watcher_locale') in LOCALES:
        result['locale'] = request.cookies['watcher_locale']
    return result


def stage_preferences(user_id, value):
    from backend.database.db import db
    from backend.database.models import AppConfig
    changes = validate_preferences(value)
    key = PREFIX + str(user_id)
    row = AppConfig.query.filter_by(key=key).first()
    previous = decode_preferences(row.value if row else None)
    encoded = json.dumps(dict(previous, **changes), ensure_ascii=True, separators=(',', ':'))
    if row:
        row.value = encoded
    else:
        db.session.add(AppConfig(key=key,value=encoded))


def translate(message, **variables):
    locale = getattr(g, 'ui_locale', None) if has_request_context() else 'zh-CN'
    text = english_catalog().get(message, message) if locale == 'en' else message
    if locale == 'en' and message not in english_catalog() and isinstance(message,str) and len(message)<2000:
        for key,names,pattern in message_patterns():
            match = pattern.fullmatch(message)
            if match:
                values = dict(zip(names,match.groups()))
                if 'count' in values and re.fullmatch(r'[\d,]+',values['count']):
                    values['count'] = int(values['count'].replace(',',''))
                return translate(key,**values)
    if isinstance(text, dict):
        text = text.get('one' if variables.get('count') == 1 else 'other', text.get('other', message))
    for key,value in variables.items():
        text = text.replace('{' + key + '}', str(value))
    return text


@lru_cache(maxsize=1)
def message_patterns():
    result=[]
    for key in english_catalog():
        names=re.findall(r'\{(\w+)\}',key)
        if names:
            fragments=re.split(r'\{\w+\}',key)
            result.append((key,names,re.compile('(.+?)'.join(map(re.escape,fragments)))))
    return result


def date_label(value):
    """Format publication dates without inventing a timezone for an official date."""
    if not value:
        return translate('时间未知')
    if isinstance(value, str):
        if value in ('今天', '昨天', '时间未知'):
            return translate(value)
        try:
            value = datetime.strptime(value, '%Y年%m月%d日')
        except ValueError:
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return value
    if has_request_context() and getattr(g, 'ui_locale', None) == 'en':
        months = ('Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec')
        suffix = value.strftime(' %H:%M') if any((value.hour,value.minute,value.second,value.microsecond)) else ''
        return f'{value.day} {months[value.month-1]} {value.year}'+suffix
    return value.strftime('%Y年%m月%d日 %H:%M' if any((value.hour,value.minute,value.second,value.microsecond)) else '%Y年%m月%d日')


def school_context(school):
    """Explicit school context; interface English never implies a US university.

    School.config.localization is an extension point for national registries and
    date adapters. Unknown country/date order remains unknown, never guessed
    from a domain, language choice or ambiguous numeric publication date.
    """
    config = school.config if isinstance(school.config,dict) else {}
    context = config.get('localization') or {}
    if not isinstance(context,dict):
        context = {}
    country = context.get('country_code')
    country = country if isinstance(country,str) and len(country)==2 and country.isascii() and country.isalpha() else None
    order = context.get('date_order')
    return {'country_code':country.upper() if country else None,
            'content_locale':context.get('content_locale') if isinstance(context.get('content_locale'),str) else None,
            'time_zone':context.get('time_zone') if isinstance(context.get('time_zone'),str) else None,
            'date_order':order if order in ('ymd','dmy','mdy') else 'unknown'}


def register_localization(app):
    from flask import Blueprint,jsonify
    from backend.auth import login_required
    from backend.database.db import db
    bp = Blueprint('localization',__name__)
    # Imported Jinja macros must translate too, including imports without context.
    app.jinja_env.globals['tr'] = translate
    app.jinja_env.globals['ui_date'] = date_label

    @bp.route('/api/preferences/localization',methods=['POST'])
    @login_required
    def save():
        try:
            changes = validate_preferences(request.get_json(silent=True))
        except ValueError as exc:
            return jsonify(error=str(exc)),400
        stage_preferences(g.user.id,changes)
        db.session.commit()
        value = preferences(g.user.id)
        response = jsonify(success=True,preferences=value)
        response.headers['Cache-Control']='no-store'
        response.set_cookie('watcher_locale',value['locale'],max_age=365*86400,samesite='Lax',
                            secure=app.config['SESSION_COOKIE_SECURE'])
        return response

    app.register_blueprint(bp)

    @app.after_request
    def language_header(response):
        # Only application error fields are translated; notice and website data stay original.
        if request.path.startswith('/api/') and response.is_json and response.status_code >= 400:
            data = response.get_json(silent=True)
            if isinstance(data, dict) and isinstance(data.get('error'), str):
                g.ui_locale = preferences()['locale']
                data['error'] = translate(data['error'])
                response.set_data(app.json.dumps(data))
        if getattr(g,'ui_locale',None):
            response.headers['Content-Language']=g.ui_locale
        return response

    @app.before_request
    def select_locale():
        # API progress messages use the same user locale as the HTML that displays them.
        # Source titles, content, IDs, URL values and persistent machine states stay original.
        if request.endpoint == 'static':
            return
        value=preferences()
        g.ui_locale=value['locale']
        g.ui_preferences=value

    @app.context_processor
    def template_locale():
        value=getattr(g,'ui_preferences',None) or preferences()
        g.ui_locale=value['locale']
        return {'tr':translate,'ui_locale':value['locale'],'ui_preferences':value,
                'ui_english_catalog':english_catalog()}
