"""Starting settings for measured source discovery, not universal optimums.

Explicit existing user settings take precedence at their established call sites.
Deployments can override these trial defaults in DISCOVERY_POLICY as one mapping.
"""
import json
import math
import os

from flask import current_app, has_app_context


DEFAULTS = {
    'fetch_global_concurrency': 2,
    'fetch_host_concurrency': 1,
    'fetch_host_interval_seconds': 1.0,
    'browser_concurrency': 1,
    'ai_global_concurrency': 2,
    'ai_school_concurrency': 1,
    'http_connect_seconds': 6,
    'http_read_seconds': 16,
    'http_total_seconds': 45,
    'browser_total_seconds': 45,
    'html_max_bytes': 3 * 1024 * 1024,
    'discovery_slice_pages': 3,
    'navigation_batch_size': 8,
    'evidence_max_bytes': 24 * 1024,
    'ai_output_tokens': 4096,
    'ai_repair_output_tokens': 8192,
    'ai_temperature': 0.1,
    'ai_decision_read_limit': 3,
    'progress_check_decisions': 4,
    'progress_no_change_checks': 2,
    'ai_school_request_limit': 40,
    'ai_school_token_limit': 200000,
    'network_attempts': 3,
    'network_retry_seconds': (30, 120),
    'network_retry_jitter': 0.2,
    'recovery_seconds': 6 * 60 * 60,
    'collection_interval_seconds': 30 * 60,
    'collection_jitter': 0.1,
    'article_sample_size': 3,
    'article_sample_attempts': 5,
    'article_sample_matches': 2,
}


def get_policy():
    # The same explicit setting reaches web, worker and isolated browser
    # processes. In particular a 90-second browser experiment must configure
    # the service too; a request alone cannot lift its default 45-second limit.
    shared = json.loads(os.environ.get('WATCHER_DISCOVERY_POLICY') or '{}')
    if not isinstance(shared, dict):
        raise ValueError('WATCHER_DISCOVERY_POLICY must be a JSON object')
    shared = {key: value for key, value in shared.items() if key in DEFAULTS}
    for key, value in shared.items():
        if isinstance(DEFAULTS[key], tuple) and not isinstance(value, (list, tuple)):
            raise ValueError('Discovery policy value must be an array: ' + key)
        numbers = value if isinstance(DEFAULTS[key], tuple) and isinstance(value, (list, tuple)) else [value]
        if not numbers or any(isinstance(number, bool) or not isinstance(number, (int, float)) or
                              not math.isfinite(number) or number < 0 for number in numbers):
            raise ValueError('Invalid discovery policy value: ' + key)
        if any(number == 0 for number in numbers) and key not in (
                'network_retry_jitter', 'collection_jitter', 'ai_temperature', 'fetch_host_interval_seconds'):
            raise ValueError('Discovery policy value must be positive: ' + key)
        if key in ('network_retry_jitter', 'collection_jitter') and value > 1:
            raise ValueError('Discovery jitter must be between zero and one')
        if isinstance(DEFAULTS[key], int) and not key.endswith('_seconds') and not isinstance(value, int):
            raise ValueError('Discovery policy value must be an integer: ' + key)
    overrides = current_app.config.get('DISCOVERY_POLICY', {}) if has_app_context() else {}
    return dict(DEFAULTS, **dict(shared, **{key: value for key, value in overrides.items() if key in DEFAULTS}))


def progress_state(progress_ids=()):
    """Seed a branch from already known, machine-observed evidence."""
    seen = sorted(set(str(value) for value in progress_ids))
    return {'seen': seen, 'decision_ids': [], 'decisions': 0,
            'checked_decisions': 0, 'last_check_count': len(seen),
            'no_change_checks': 0, 'stopped': False}


def advance_progress(state, decision_id, progress_ids=()):
    """Count distinct model decisions and actual results, never explanations.

    Callers supply stable IDs for successfully read material, validated columns,
    matched articles or newly installed records. Failed reads and proposed CSS
    are not results. Replaying a paid execution cannot advance this clock.
    """
    current = dict(state or progress_state())
    current['seen'] = sorted(set(current.get('seen', [])) | {str(value) for value in progress_ids})
    decisions = list(current.get('decision_ids', []))
    if str(decision_id) in decisions or current.get('stopped'):
        return current
    decisions.append(str(decision_id))
    current.update(decision_ids=decisions, decisions=len(decisions))
    policy = get_policy()
    interval = max(1, int(policy['progress_check_decisions']))
    if current['decisions'] - current.get('checked_decisions', 0) >= interval:
        increased = len(current['seen']) > current.get('last_check_count', 0)
        misses = 0 if increased else current.get('no_change_checks', 0) + 1
        current.update(checked_decisions=current['decisions'], last_check_count=len(current['seen']),
                       no_change_checks=misses,
                       stopped=misses >= max(1, int(policy['progress_no_change_checks'])))
    return current
