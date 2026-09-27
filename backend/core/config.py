"""配置与路径常量 + YAML 种子导入"""
import os
import logging
import secrets
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

# ---- 路径常量 ----
# 本文件位于 backend/core/，其上两级即项目根目录
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(os.environ.get('WATCHER_DATA_DIR', str(ROOT_DIR / 'data'))).resolve()
CONFIG_YAML_PATH = ROOT_DIR / 'config.yaml'
ENV_PATH = ROOT_DIR / '.env'

DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_database_uri():
    """数据库连接串：优先 DATABASE_URL 环境变量，否则默认 data/school_watcher.db"""
    env_uri = os.environ.get('DATABASE_URL')
    if env_uri:
        if env_uri.startswith('postgres://'):
            env_uri = 'postgresql+psycopg://' + env_uri[len('postgres://'):]
        elif env_uri.startswith('postgresql://'):
            env_uri = 'postgresql+psycopg://' + env_uri[len('postgresql://'):]
        return env_uri
    return f'sqlite:///{DATA_DIR / "school_watcher.db"}'


def ensure_secret_key():
    """SECRET_KEY：优先环境变量，否则自动生成并持久化到 .env"""
    sk = os.environ.get('SECRET_KEY')
    if sk:
        return sk

    if os.environ.get('WATCHER_ENV') == 'production':
        raise RuntimeError('SECRET_KEY must be configured for production')
    from dotenv import dotenv_values
    saved = dotenv_values(ENV_PATH).get('SECRET_KEY') if ENV_PATH.exists() else None
    if saved:
        os.environ['SECRET_KEY'] = saved
        return saved
    sk = secrets.token_hex(32)
    os.environ['SECRET_KEY'] = sk
    # 尝试写入 .env 文件以便重启后复用
    try:
        existing = ENV_PATH.read_text(encoding='utf-8') if ENV_PATH.exists() else ''
        if 'SECRET_KEY=' not in existing:
            with open(ENV_PATH, 'a', encoding='utf-8') as f:
                f.write(f'\nSECRET_KEY={sk}\n')
            logger.info("已自动生成 SECRET_KEY 并保存到 .env")
    except Exception:
        pass
    return sk



def ensure_field_encryption_key():
    """Persist an instance-local key without ever logging or exporting its value."""
    if os.environ.get('FIELD_ENC_KEY'):
        return
    if os.environ.get('WATCHER_ENV') == 'production':
        raise RuntimeError('FIELD_ENC_KEY must be configured for production')
    from filelock import FileLock
    target = DATA_DIR / '.field-key'
    try:
        with FileLock(str(target) + '.lock', timeout=10):
            if target.exists():
                key = target.read_text(encoding='ascii').strip()
                if len(key) < 32:
                    raise RuntimeError('Local encryption key file is invalid; restore it or reconfigure credentials')
            else:
                key = secrets.token_hex(32)
                descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w', encoding='ascii') as writer:
                    writer.write(key + '\n')
                    writer.flush()
                    os.fsync(writer.fileno())
            os.environ['FIELD_ENC_KEY'] = key
    except (OSError, TimeoutError) as exc:
        raise RuntimeError('Cannot persist the instance encryption key; configure FIELD_ENC_KEY') from exc


def load_config_yaml():
    """从 config.yaml 自动导入学校配置到数据库（只增不更新）"""
    from backend.database.db import db
    from backend.database.models import School, Department

    if not CONFIG_YAML_PATH.exists():
        logger.warning("config.yaml 不存在，跳过自动导入")
        return

    try:
        with open(CONFIG_YAML_PATH, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"读取 config.yaml 失败: {e}")
        return

    if not config or 'schools' not in config:
        return

    from backend.services.school_registry import ensure_school
    from backend.services.source_inventory import canonical_url
    from backend.services.source_governance import queue_source_review
    for school_cfg in config['schools']:
        if not school_cfg.get('name'):
            continue
        school, created = ensure_school(school_cfg['name'], school_cfg.get('url', ''), origin='seed')
        if created:
            school.enabled = school_cfg.get('enabled', True)
            db.session.commit()
        for dept_cfg in school_cfg.get('departments', []):
            if not dept_cfg.get('name'):
                continue
            existing = next((dept for dept in school.departments if dept.name == dept_cfg['name']
                and canonical_url(dept.list_url or '') == canonical_url(dept_cfg.get('list_url', ''))), None)
            if existing:
                # Existing installed rules and source identities are untouched.
                continue
            dept = Department(school_id=school.id, name=dept_cfg['name'],
                              list_url=dept_cfg.get('list_url', ''), group_name=dept_cfg.get('group_name', ''))
            db.session.add(dept)
            db.session.commit()
            if dept_cfg.get('list_url'):
                try:
                    queue_source_review(school.id, dept_cfg, department_id=dept.id)
                except ValueError as exc:
                    logger.warning('YAML 来源仍待核实：%s (%s)', dept.name, exc)
            logger.info('从 YAML 导入待核实来源：%s → %s', school.name, dept.name)
