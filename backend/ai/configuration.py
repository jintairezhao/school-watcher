"""Instance-only configuration; this module never exposes plaintext credentials."""
from datetime import datetime
import hashlib
import re
from backend.database.db import db
from backend.core.secrets import decrypt_field, encrypt_field, encryption_available
from backend.ai.models import AIProfile, AIBinding, AIBudget
from backend.ai.providers import ENDPOINTS, PROVIDER_NAMES, DEFAULT_REGIONS, endpoint_for

PURPOSES = ('directory', 'summary')


class AIConfigError(ValueError):
    def __init__(self, message, code='configuration_invalid'):
        super().__init__(message)
        self.code = code


def _metadata(profile):
    return {'id': profile.id, 'profile_id': profile.id, 'name': profile.name,
            'version': profile.version, 'provider': profile.provider, 'model': profile.model,
            'region': profile.region, 'max_output_tokens': profile.max_output_tokens,
            'enabled': profile.enabled, 'tested': profile.tested_version == profile.version,
            'has_key': bool(profile.encrypted_key), 'last_test_code': profile.last_test_code}


def _legacy_binding():
    from backend.database.models import AppConfig
    stored = AppConfig.get('deepseek_api_key') or ''
    if not stored:
        return None
    # Key fingerprint is used internally as configuration version, never the key itself.
    return {'id': 0, 'profile_id': 0, 'name': '原有 DeepSeek 配置',
            'version': 'legacy-' + hashlib.sha256(stored.encode()).hexdigest()[:16],
            'provider': 'deepseek', 'model': 'deepseek-chat', 'region': 'default',
            'max_output_tokens': 4096, 'enabled': True, 'tested': False,
            'has_key': True, 'legacy': True}


def get_model_binding(purpose):
    if purpose not in PURPOSES:
        raise AIConfigError('未知 AI 用途')
    from backend.database.models import AppConfig
    if AppConfig.get('ai_restore_review_required') == '1':
        raise AIConfigError('恢复备份后请管理员重新测试 AI 配置并核对原调用', 'restored_requires_review')
    binding = db.session.get(AIBinding, purpose)
    if binding:
        profile = db.session.get(AIProfile, binding.profile_id)
        if not profile or not profile.enabled or profile.tested_version != profile.version:
            raise AIConfigError('AI 配置未启用或变更后尚未测试', 'profile_disabled')
        return _metadata(profile)
    # Only installations with no new explicit configuration use historical settings.
    if purpose == 'summary' and not AIProfile.query.first():
        legacy = _legacy_binding()
        if legacy:
            return legacy
    raise AIConfigError('请管理员配置本实例的 AI 服务', 'not_configured')


resolve_profile = get_model_binding


def credential_for(binding, *, allow_untested=False):
    """Check revocation/version immediately before dispatch; do not serialize result."""
    if binding.get('id', binding.get('profile_id')) == 0:
        from backend.database.models import AppConfig
        current = _legacy_binding()
        if not current or current['version'] != binding['version'] or AIProfile.query.first():
            raise AIConfigError('AI 配置已变更，请重新发起操作', 'configuration_changed')
        key = decrypt_field(AppConfig.get('deepseek_api_key') or '')
    else:
        profile = db.session.get(AIProfile, int(binding.get('id', binding.get('profile_id'))), populate_existing=True)
        if not profile or profile.version != binding['version']:
            raise AIConfigError('AI 配置已变更，请重新发起操作', 'configuration_changed')
        if not allow_untested and (not profile.enabled or profile.tested_version != profile.version):
            raise AIConfigError('AI 配置已停用', 'profile_disabled')
        # Check locked public metadata too, so a caller cannot redirect an authenticated key.
        for field in ('provider', 'model', 'region'):
            if binding.get(field) != getattr(profile, field):
                raise AIConfigError('AI 配置引用不匹配', 'configuration_changed')
        key = decrypt_field(profile.encrypted_key)
    if not key:
        raise AIConfigError('API 密钥不可用，请检查加密主密钥或重新配置', 'credential_unavailable')
    return key


def public_settings():
    from backend.database.models import AppConfig
    bindings = {row.purpose: row.profile_id for row in AIBinding.query.all()}
    profiles = [_metadata(p) for p in AIProfile.query.order_by(AIProfile.id).all()]
    legacy = _legacy_binding() if not profiles else None
    return {'providers': [{'id': key, 'name': PROVIDER_NAMES[key], 'regions': list(value)}
                          for key, value in ENDPOINTS.items()],
            'profiles': profiles, 'bindings': bindings, 'legacy': legacy,
            'encryption_available': encryption_available(),
            'limits': {purpose: int(AppConfig.get('ai_token_limit_' + purpose, '0') or 0)
                       for purpose in ('total',) + PURPOSES},
            'max_concurrent': int(AppConfig.get('ai_max_concurrent', '2') or 2),
            'usage': [{'key': row.key, 'used_tokens': row.used_tokens,
                       'reserved_tokens': row.reserved_tokens, 'active_count': row.active_count}
                      for row in AIBudget.query.order_by(AIBudget.key.desc()).limit(6)]}


def save_profile(payload, profile_id=None):
    if not isinstance(payload, dict):
        raise AIConfigError('配置必须是对象')
    allowed = {'name', 'provider', 'model', 'region', 'api_key', 'max_output_tokens', 'enabled', 'expected_version'}
    if set(payload) - allowed:
        raise AIConfigError('包含不支持的配置字段')
    row = db.session.get(AIProfile, profile_id) if profile_id else None
    if profile_id and not row:
        raise AIConfigError('配置不存在', 'not_found')
    if row and payload.get('expected_version') is not None and payload['expected_version'] != row.version:
        raise AIConfigError('配置已被其他管理员更新', 'configuration_changed')
    provider = payload.get('provider', row.provider if row else '')
    region = payload.get('region', row.region if row and provider == row.provider else DEFAULT_REGIONS.get(provider, ''))
    endpoint_for(provider, region)
    model = str(payload.get('model', row.model if row else '')).strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}', model):
        raise AIConfigError('请填写有效的模型或接入点标识')
    name = str(payload.get('name', row.name if row else PROVIDER_NAMES[provider])).strip()
    if not name or len(name) > 100:
        raise AIConfigError('配置名称长度应为 1–100 字符')
    maximum = int(payload.get('max_output_tokens', row.max_output_tokens if row else 4096))
    if not 256 <= maximum <= 16384:
        raise AIConfigError('输出预算应为 256–16384 tokens')
    raw_key = payload.get('api_key')
    if raw_key is not None and (not isinstance(raw_key, str) or not 8 <= len(raw_key.strip()) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in raw_key.strip())):
        raise AIConfigError('请填写有效 API 密钥')
    if row is None and raw_key is None:
        raise AIConfigError('新配置需要 API 密钥')
    encrypted = encrypt_field(raw_key.strip()) if raw_key is not None else row.encrypted_key
    changed = row is None or any([row.provider != provider, row.region != region, row.model != model,
                                 row.max_output_tokens != maximum, raw_key is not None])
    if row is None:
        row = AIProfile(version=1, enabled=False)
        db.session.add(row)
    elif changed:
        row.version += 1
    row.name, row.provider, row.region, row.model = name, provider, region, model
    row.encrypted_key, row.max_output_tokens, row.updated_at = encrypted, maximum, datetime.utcnow()
    if changed:
        row.enabled, row.tested_version, row.last_test_code = False, None, 'not_tested'
    elif 'enabled' in payload:
        if payload['enabled'] and row.tested_version != row.version:
            raise AIConfigError('请先完成连接与结构化输出测试', 'not_tested')
        row.enabled = bool(payload['enabled'])
    db.session.commit()
    return _metadata(row)


def bind_profile(purpose, profile_id):
    if purpose not in PURPOSES:
        raise AIConfigError('未知 AI 用途')
    profile = db.session.get(AIProfile, int(profile_id))
    if not profile or not profile.enabled or profile.tested_version != profile.version:
        raise AIConfigError('请先测试并启用服务配置', 'not_tested')
    row = db.session.get(AIBinding, purpose)
    if row is None:
        db.session.add(AIBinding(purpose=purpose, profile_id=profile.id))
    else:
        row.profile_id = profile.id
    db.session.commit()
    return get_model_binding(purpose)


def delete_profile(profile_id):
    row = db.session.get(AIProfile, int(profile_id))
    if row is None:
        raise AIConfigError('配置不存在', 'not_found')
    AIBinding.query.filter_by(profile_id=row.id).delete()
    # Retain provenance and prevent legacy fallback after intentional revocation.
    row.encrypted_key, row.enabled, row.tested_version = '', False, None
    row.version += 1
    db.session.commit()
    return {'revoked': True, 'id': row.id}


def save_limits(payload):
    from backend.database.models import AppConfig
    allowed = {'total', 'directory', 'summary', 'max_concurrent'}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise AIConfigError('额度配置无效')
    values = {}
    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
            raise AIConfigError('额度必须为非负整数；0 表示不设消费上限')
        if key == 'max_concurrent' and not 1 <= value <= 16:
            raise AIConfigError('同时执行数应为 1–16')
        values['ai_max_concurrent' if key == 'max_concurrent' else 'ai_token_limit_' + key] = str(value)
    for key, value in values.items():
        row = AppConfig.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(AppConfig(key=key, value=value))
    db.session.commit()
    return public_settings()


def test_profile(profile_id):
    from backend.ai.runtime import test_connection
    return test_connection(profile_id)


def execution_history(limit=50):
    """Only operational metadata, never prompts, source text, model output or keys."""
    from backend.ai.models import AIExecution
    limit = max(1, min(100, int(limit)))
    return {'executions': [{
        'execution_id': row.execution_id, 'purpose': row.purpose, 'provider': row.provider,
        'model': row.model, 'status': row.status, 'usage': row.usage or {},
        'reserved_tokens': row.reserved_tokens, 'error_code': row.error_code,
        'request_id': row.request_id,
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'finished_at': row.finished_at.isoformat() if row.finished_at else None,
    } for row in AIExecution.query.order_by(AIExecution.created_at.desc()).limit(limit)]}
