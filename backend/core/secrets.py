"""Encrypted instance credentials. Legacy plaintext reads remain compatible."""
import base64
import hashlib
import os

_fernet = None
_tried = False
_key_fingerprint = None


class EncryptionUnavailable(ValueError):
    pass


def _get_fernet():
    global _fernet, _tried, _key_fingerprint
    key = os.environ.get('FIELD_ENC_KEY', '')
    fingerprint = hashlib.sha256(key.encode()).hexdigest() if key else None
    if _tried and fingerprint == _key_fingerprint:
        return _fernet
    _tried, _key_fingerprint, _fernet = True, fingerprint, None
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        digest = hashlib.sha256(key.encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    except (ImportError, ValueError):
        return None
    return _fernet


def encryption_available():
    return _get_fernet() is not None


def encrypt_field(plain: str) -> str:
    if not plain:
        return plain
    f = _get_fernet()
    if f is None:
        raise EncryptionUnavailable('未配置有效的 FIELD_ENC_KEY，不能保存 API 密钥')
    return 'enc:' + f.encrypt(plain.encode()).decode()


def decrypt_field(value: str) -> str:
    if not value or not value.startswith('enc:'):
        return value or ''
    f = _get_fernet()
    if f is None:
        return ''
    try:
        return f.decrypt(value[4:].encode()).decode()
    except Exception:
        return ''
