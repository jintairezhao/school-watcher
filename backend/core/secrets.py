"""敏感字段加密存储（Fernet）

主密钥来自环境变量 FIELD_ENC_KEY；缺省时回落明文并告警，
保证可渐进启用、不破坏存量部署。
"""
import base64
import hashlib
import logging
import os

logger = logging.getLogger(__name__)

_fernet = None
_tried = False


def _get_fernet():
    global _fernet, _tried
    if _tried:
        return _fernet
    _tried = True
    key = os.environ.get('FIELD_ENC_KEY', '')
    if not key:
        logger.warning("未设置 FIELD_ENC_KEY，敏感字段以明文存储")
        return None
    try:
        from cryptography.fernet import Fernet
        # 任意长度口令 → 32 字节密钥
        digest = hashlib.sha256(key.encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    except Exception as e:
        logger.warning(f"Fernet 初始化失败，回落明文: {e}")
        _fernet = None
    return _fernet


def encrypt_field(plain: str) -> str:
    if not plain:
        return plain
    f = _get_fernet()
    if not f:
        return plain
    return 'enc:' + f.encrypt(plain.encode()).decode()


def decrypt_field(value: str) -> str:
    if not value or not value.startswith('enc:'):
        return value or ''
    f = _get_fernet()
    if not f:
        return ''
    try:
        return f.decrypt(value[4:].encode()).decode()
    except Exception:
        return ''
