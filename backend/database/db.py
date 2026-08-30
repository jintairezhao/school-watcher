"""数据库连接管理

`db` 实例统一定义在 backend.core，此处仅做 re-export，
供 models 及各模块 `from backend.database.db import db` 使用。
"""

from backend.core import db

__all__ = ['db']
