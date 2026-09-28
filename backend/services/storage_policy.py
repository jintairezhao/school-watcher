"""Persisted retention rules shared by the web process and independent worker."""
from flask import current_app

from backend.database.db import db
from backend.database.models import AppConfig
from backend.services.scrape_logs import retention_days


def valid_limit(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def capacity_bytes(key, config_key, default):
    if AppConfig.get(key) is None:
        return current_app.config.get(config_key, default)
    return policy()[key] * 1048576


def retention_cutoff(days, now):
    from datetime import datetime, timedelta
    floor = datetime.min.replace(tzinfo=now.tzinfo)
    return now - timedelta(days=min(days, (now - floor).days))


def policy():
    defaults = {
        'body_cache_days': current_app.config.get('BODY_CACHE_DAYS', 30),
        'discovery_cache_days': 7,
        'backup_keep_count': 7,
        'body_cache_mb': int(current_app.config.get('BODY_CACHE_BYTES', 150 * 1048576) / 1048576),
        'discovery_cache_mb': 100,
        'fetch_cache_mb': int(current_app.config.get('FETCH_EVIDENCE_BYTES', 100 * 1048576) / 1048576),
    }
    result = {}
    for key, default in defaults.items():
        try:
            value = int(AppConfig.get(key, str(default)))
        except (TypeError, ValueError):
            value = default
        result[key] = value if valid_limit(value) else default
    result['scrape_log_retention_days'] = retention_days()
    return result


def save_policy(values):
    if not isinstance(values, dict) or not values or set(values) - set(policy()):
        raise ValueError('保留规则字段不正确')
    for key, value in values.items():
        if not valid_limit(value):
            raise ValueError('请输入非负整数，0 表示不限制')
    for key, value in values.items():
        row = AppConfig.query.filter_by(key=key).first()
        if row is None:
            row = AppConfig(key=key)
            db.session.add(row)
        row.value = str(value)
    db.session.commit()
    return policy()


def cleanup(*, all_cache=False, category=None):
    from pathlib import Path
    from filelock import FileLock
    from backend.services.content_cache import prune_content
    from backend.services.discovery_cache import DiscoveryCache
    from backend.services.scrape_logs import prune_scrape_logs
    from backend.services.backups import rotate
    from backend.services.task_fetch import prune_fetch_evidence

    scratch = Path(current_app.config['DISCOVERY_CACHE_PATH'])
    # Acquire before changing anything: a running discovery must keep its frontier.
    scratch.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(scratch) + '.worker.lock', timeout=0):
        result = {'evicted': 0, 'scrape_logs_deleted': 0, 'discovery_snapshots_deleted': 0, 'backups_deleted': 0}
        if category in (None, 'body'):
            result.update(prune_content(all_cache=all_cache or category == 'body'))
        if category in (None, 'logs'):
            result['scrape_logs_deleted'] = prune_scrape_logs(all_logs=category == 'logs')
        result['discovery_snapshots_deleted'] = (
            DiscoveryCache(scratch).trim(all_cache=all_cache or category == 'discovery')
            if scratch.exists() and category in (None, 'discovery') else 0)
        if category in (None, 'fetch'):
            result['fetch_evidence'] = prune_fetch_evidence(all_cache=all_cache or category == 'fetch')
        from backend.services.source_governance import prune_governance_evidence
        if category in (None, 'governance'):
            result['governance_evidence'] = prune_governance_evidence(
                policy()['discovery_cache_days'], all_cache=all_cache or category == 'governance')
        from backend.services.runtime_catalog import prune_catalog_generations
        if category is None:
            result['catalog_generations'] = prune_catalog_generations()
        result['backups_deleted'] = 0
        for folder in {current_app.config.get('BACKUP_DIR'), current_app.config.get('BACKUP_COPY_DIR')}:
            if folder and category in (None, 'backups'):
                result['backups_deleted'] += rotate(Path(folder).resolve(), clear=category == 'backups')
    return result
