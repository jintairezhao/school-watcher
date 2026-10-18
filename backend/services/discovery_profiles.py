"""Personal role selections, composed once per subscriber for shared discovery."""
import json

from flask import current_app, has_app_context
from backend.scraper.discovery.attention import (ALL_PROFILES, CATEGORY_KEYS, PROFILE_LABELS,
                                                 normalize_profiles, compose_profiles)

PREFIX = 'discovery_profiles:user:'


def validate_profiles(value):
    if (not isinstance(value, list) or not value or len(value) > 3
            or any(not isinstance(item, str) or item not in ALL_PROFILES for item in value)):
        raise ValueError('请至少选择一种有效身份，可同时选择本科生、研究生和教师')
    return list(normalize_profiles(value))


def _read(raw):
    try:
        data = json.loads(raw or '{}')
        return validate_profiles(data.get('profiles')) if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def user_profiles(user_id):
    from backend.database.models import AppConfig
    return _read(AppConfig.get(PREFIX + str(user_id)))


def stage_profiles(user_id, profiles):
    """Join the caller's transaction; an invalid setup must not save a profile."""
    from backend.database.db import db
    from backend.database.models import AppConfig
    chosen = validate_profiles(profiles)
    key = PREFIX + str(user_id)
    row = AppConfig.query.filter_by(key=key).first()
    value = json.dumps({'version': 1, 'profiles': chosen}, separators=(',', ':'))
    if row:
        row.value = value
    else:
        db.session.add(AppConfig(key=key, value=value))
    return chosen


def subscriber_policy(school_id=None, *, root_url=None):
    """Use configured subscribers only; unknown identities aren't guessed.

    Each subscriber contributes one composite, even with multiple identities.
    Need weights average the contributors to that need, rather than multiplying
    the number of workers, pages, subscriptions or requests by identity count.
    """
    if not has_app_context() or 'sqlalchemy' not in current_app.extensions:
        return None
    from backend.database.db import db
    from backend.database.models import School, Subscription, AppConfig
    if school_id is None:
        from backend.services.tasks import current_execution
        handle = current_execution()
        school_id = ((handle or {}).get('payload') or {}).get('school_id')
    if type(school_id) is int:
        school_ids = [school_id]
    elif root_url:
        from backend.services.source_inventory import canonical_url
        root = canonical_url(root_url)
        school_ids = [row.id for row in db.session.query(School.id, School.url).filter(School.enabled.is_(True))
                      if canonical_url(row.url) == root]
    else:
        return None
    if not school_ids:
        return None
    user_ids = {row[0] for row in db.session.query(Subscription.user_id).filter(
        Subscription.school_id.in_(school_ids)).distinct()}
    if not user_ids:
        return None
    from backend.services.localization import PREFIX as LOCALE_PREFIX, decode_preferences
    keys = [prefix + str(ident) for ident in user_ids for prefix in (PREFIX,LOCALE_PREFIX)]
    stored = {}
    composites = []
    for offset in range(0, len(keys), 200):
        stored.update(db.session.query(AppConfig.key,AppConfig.value).filter(AppConfig.key.in_(keys[offset:offset + 200])).all())
    for ident in sorted(user_ids):
        profiles = _read(stored.get(PREFIX+str(ident)))
        if profiles:
            item = compose_profiles(profiles)
            item['education_system'] = decode_preferences(stored.get(LOCALE_PREFIX+str(ident))).get('education_system','cn')
            composites.append(item)
    if not composites:
        return None
    profiles = [profile for profile in ALL_PROFILES if any(profile in item['profiles'] for item in composites)]
    weights = {}
    for category in CATEGORY_KEYS:
        contributions = [item['weights'][category] for item in composites if category in item['weights']]
        weights[category] = sum(contributions) / len(contributions) if contributions else 1.0
    groups = {}
    for item in composites:
        group_key=item['key']+'@'+item['education_system']
        group = groups.setdefault(group_key, {'profiles': item['profiles'], 'label': item['label'],
            'education_system':item['education_system'], 'count': 0})
        group['count'] += 1
    return {'profiles': profiles, 'weights': weights, 'source': 'subscriber_profiles',
            'subscriber_count': len(composites), 'groups': [groups[key] for key in sorted(groups)]}


def profile_choices():
    return [{'id': key, 'label': PROFILE_LABELS[key]} for key in ALL_PROFILES]


def composite_previews():
    from itertools import combinations
    return [compose_profiles(list(group)) for size in (1, 2, 3) for group in combinations(ALL_PROFILES, size)]
