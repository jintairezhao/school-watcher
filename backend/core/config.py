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
DATA_DIR = ROOT_DIR / 'data'
CONFIG_YAML_PATH = ROOT_DIR / 'config.yaml'
ENV_PATH = ROOT_DIR / '.env'

DATA_DIR.mkdir(exist_ok=True)


def get_database_uri():
    """数据库连接串：优先 DATABASE_URL 环境变量，否则默认 data/school_watcher.db"""
    env_uri = os.environ.get('DATABASE_URL')
    if env_uri:
        return env_uri
    return f'sqlite:///{DATA_DIR / "school_watcher.db"}'


def ensure_secret_key():
    """SECRET_KEY：优先环境变量，否则自动生成并持久化到 .env"""
    sk = os.environ.get('SECRET_KEY')
    if sk:
        return sk

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

    for school_cfg in config['schools']:
        if not school_cfg.get('name'):
            continue

        existing = School.query.filter_by(name=school_cfg['name']).first()
        if existing:
            # 检查是否有新部门需要添加
            for dept_cfg in school_cfg.get('departments', []):
                dept_exists = Department.query.filter_by(
                    school_id=existing.id, name=dept_cfg['name']
                ).first()
                if not dept_exists:
                    dept = Department(
                        school_id=existing.id,
                        name=dept_cfg['name'],
                        list_url=dept_cfg.get('list_url', ''),
                        list_selector=dept_cfg.get('list_selector', ''),
                        title_selector=dept_cfg.get('title_selector', ''),
                        link_selector=dept_cfg.get('link_selector', ''),
                        date_selector=dept_cfg.get('date_selector', ''),
                        content_selector=dept_cfg.get('content_selector', ''),
                    )
                    db.session.add(dept)
                    logger.info(f"从 YAML 添加部门: {school_cfg['name']} → {dept_cfg['name']}")
            db.session.commit()
            continue

        # 创建新学校
        school = School(
            name=school_cfg['name'],
            url=school_cfg.get('url', ''),
            enabled=school_cfg.get('enabled', True),
        )
        db.session.add(school)
        db.session.flush()  # 获取 school.id

        for dept_cfg in school_cfg.get('departments', []):
            dept = Department(
                school_id=school.id,
                name=dept_cfg['name'],
                list_url=dept_cfg.get('list_url', ''),
                list_selector=dept_cfg.get('list_selector', ''),
                title_selector=dept_cfg.get('title_selector', ''),
                link_selector=dept_cfg.get('link_selector', ''),
                date_selector=dept_cfg.get('date_selector', ''),
                content_selector=dept_cfg.get('content_selector', ''),
            )
            db.session.add(dept)

        db.session.commit()
        logger.info(f"从 YAML 导入学校: {school_cfg['name']} ({len(school_cfg.get('departments', []))} 个部门)")
