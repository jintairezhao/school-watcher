"""Persisted publication cutoff, shared by manual and scheduled collection."""
from datetime import date, datetime
import re
import hashlib
import json

from backend.database.models import AppConfig


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
    return datetime.strptime(since_month(), '%Y-%m')


def source_signature(department):
    fields = ('list_url', 'list_selector', 'title_selector', 'link_selector', 'date_selector')
    return hashlib.sha256(json.dumps([getattr(department, f) for f in fields]).encode()).hexdigest()


def coverage_complete(department, cutoff):
    try:
        value = json.loads(AppConfig.get(f'collection_coverage:{department.id}', '{}'))
        return (value.get('version') == 1 and value.get('signature') == source_signature(department)
                and datetime.strptime(value['since'], '%Y-%m') <= cutoff)
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


def record_coverage(department, cutoff):
    AppConfig.set(f'collection_coverage:{department.id}', json.dumps({
        'version': 1, 'signature': source_signature(department), 'since': cutoff.strftime('%Y-%m')}))
