"""Persisted retention rules shared by the web process and independent worker."""
from flask import current_app

from backend.database.db import db
from backend.database.models import AppConfig
from backend.services.scrape_logs import RETENTION_CHOICES, retention_days


def policy():
    defaults = {
        'body_cache_days': current_app.config.get('BODY_CACHE_DAYS', 30),
        'discovery_cache_days': 7,
        'backup_keep_count': 7,
    }
    result = {}
    for key, default in defaults.items():
        try:
            value = int(AppConfig.get(key, str(default)))
        except (TypeError, ValueError):
            value = default
        low, high = (1, 100) if key == 'backup_keep_count' else (0, 3650)
        result[key] = value if low <= value <= high else default
    result['scrape_log_retention_days'] = retention_days()
    return result


def save_policy(values):
    if not isinstance(values, dict) or set(values) != set(policy()):
        raise ValueError('请完整填写四项保留规则')
    for key, value in values.items():
        if type(value) is not int:
            raise ValueError('保留天数和备份份数必须为整数')
        if key == 'scrape_log_retention_days':
            if value not in RETENTION_CHOICES:
                raise ValueError('请选择有效的抓取记录保留时长')
        elif key == 'backup_keep_count':
            if not 1 <= value <= 100:
                raise ValueError('自动备份保留份数应为 1–100')
        elif not 0 <= value <= 3650:
            raise ValueError('缓存保留天数应为 0–3650，0 表示不按时间清理')
    for key, value in values.items():
        row = AppConfig.query.filter_by(key=key).first()
        if row is None:
            row = AppConfig(key=key)
            db.session.add(row)
        row.value = str(value)
    db.session.commit()
    return policy()


def cleanup(*, all_cache=False):
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
        result = prune_content(all_cache=all_cache)
        result['scrape_logs_deleted'] = prune_scrape_logs()
        result['discovery_snapshots_deleted'] = (
            DiscoveryCache(scratch).trim(all_cache=all_cache) if scratch.exists() else 0)
        result['fetch_evidence'] = prune_fetch_evidence(all_cache=all_cache)
        from backend.services.source_governance import prune_governance_evidence
        result['governance_evidence'] = prune_governance_evidence(
            policy()['discovery_cache_days'], all_cache=all_cache)
        from backend.services.runtime_catalog import prune_catalog_generations
        result['catalog_generations'] = prune_catalog_generations()
        result['backups_deleted'] = 0
        for folder in {current_app.config.get('BACKUP_DIR'), current_app.config.get('BACKUP_COPY_DIR')}:
            if folder:
                result['backups_deleted'] += rotate(Path(folder).resolve())
    return result
