"""Persisted publication cutoff, shared by manual and scheduled collection."""
from datetime import date, datetime
import re
import hashlib
import json

from backend.database.models import AppConfig


def validate_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('请选择有效的起始发布日期')
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError('请选择有效的起始发布日期') from None
    if parsed.year < 1900 or parsed > date.today():
        raise ValueError('起始发布日期需在 1900 年至今天之间')
    return value


def validate_settings(data):
    """Validate every field before persisting any of them."""
    cutoff, interval = None, None
    if 'since_date' in data:
        cutoff = validate_date(data['since_date'])
        if 'since_month' in data and validate_month(data['since_month']) != cutoff[:7]:
            raise ValueError('起始日期与起始年月不一致')
    elif 'since_month' in data:
        cutoff = validate_month(data['since_month']) + '-01'
    if 'interval' in data:
        raw = data['interval']
        if type(raw) is not int and not (isinstance(raw, str) and re.fullmatch(r'\d+', raw)):
            raise ValueError('间隔必须为整数（分钟）')
        interval = int(raw)
        if not 5 <= interval <= 720:
            raise ValueError('间隔需在 5-720 分钟之间')
    return cutoff, interval


def save_settings(cutoff, interval, *, complete=False, ai_mode=None):
    from backend.database.db import db
    from backend.database.models import Department
    values = {}
    if cutoff is not None:
        if cutoff != since_date().strftime('%Y-%m-%d'):
            Department.query.update({Department.last_scraped_at: None})
        values.update(scrape_since_date=cutoff, scrape_since_month=cutoff[:7])
    if interval is not None:
        values['scrape_interval'] = str(interval)
    if complete:
        values['collection_setup_completed'] = '1'
    if ai_mode is not None:
        values['collection_ai_mode'] = ai_mode
    for key, value in values.items():
        row = AppConfig.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(AppConfig(key=key, value=value))
    db.session.commit()


def setup_required():
    """Fresh installations need confirmation; upgrades retain their subscriptions."""
    from backend.database.models import Subscription
    return AppConfig.get('collection_setup_completed') != '1' and not Subscription.query.first()


def setup_guard():
    from flask import jsonify, url_for
    if setup_required():
        return jsonify(error='请先完成基础配置，再开始订阅或添加学校',
                       error_code='collection_setup_required',
                       setup_url=url_for('collection_setup.page')), 409


def validate_month(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}', value):
        raise ValueError('请选择有效的起始年月')
    try:
        first = datetime.strptime(value, '%Y-%m').date()
    except ValueError:
        raise ValueError('请选择有效的起始年月') from None
    if first.year < 1900 or first > date.today().replace(day=1):
        raise ValueError('起始年月需在 1900 年至本月之间')
    return value


def since_month():
    # Keep several application/award cycles available for preparation. This is
    # a user-adjustable collection window, never a judgment that old is useless.
    default = f'{date.today().year - 3}-01'
    try:
        return validate_month(AppConfig.get('scrape_since_month', default))
    except ValueError:
        return default


def since_date():
    exact = AppConfig.get('scrape_since_date')
    if exact:
        try:
            # A legacy month-only edit must not leave an incompatible exact day.
            if exact[:7] == since_month():
                return datetime.fromisoformat(validate_date(exact))
        except (ValueError, TypeError):
            pass
    return datetime.strptime(since_month(), '%Y-%m')


def source_signature(department):
    fields = ('list_url', 'list_selector', 'title_selector', 'link_selector', 'date_selector')
    return hashlib.sha256(json.dumps([getattr(department, f) for f in fields]).encode()).hexdigest()


def coverage_complete(department, cutoff):
    try:
        value = json.loads(AppConfig.get(f'collection_coverage:{department.id}', '{}'))
        stored = (datetime.fromisoformat(value['since']) if value.get('version') == 2
                  else datetime.strptime(value['since'], '%Y-%m'))
        return (value.get('version') in (1, 2) and value.get('signature') == source_signature(department)
                and stored <= cutoff)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def record_coverage(department, cutoff):
    AppConfig.set(f'collection_coverage:{department.id}', json.dumps({
        'version': 2, 'signature': source_signature(department), 'since': cutoff.strftime('%Y-%m-%d')}))
