"""核心基础设施：路径常量、配置、Flask 扩展单例"""
from backend.core.config import (
    ROOT_DIR,
    DATA_DIR,
    CONFIG_YAML_PATH,
    ENV_PATH,
    get_database_uri,
    ensure_secret_key,
    load_config_yaml,
)
from backend.core.extensions import db, migrate

__all__ = [
    'ROOT_DIR',
    'DATA_DIR',
    'CONFIG_YAML_PATH',
    'ENV_PATH',
    'get_database_uri',
    'ensure_secret_key',
    'load_config_yaml',
    'db',
    'migrate',
]
