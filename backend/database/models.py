"""数据库 ORM 模型定义"""

from datetime import datetime, timezone
from backend.database.db import db


class School(db.Model):
    """学校"""
    __tablename__ = 'schools'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False, comment='学校名称')
    url = db.Column(db.String(500), nullable=False, comment='学校官网URL')
    enabled = db.Column(db.Boolean, default=True, comment='admin 上架/下架开关')
    config = db.Column(db.Text, comment='爬取配置JSON')
    submitted_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True,
                             comment='提交人（存量学校为 NULL）')
    subscriber_count = db.Column(db.Integer, nullable=False, server_default='0',
                                 comment='订阅人数缓存（订阅事务内算术维护）')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    submitter = db.relationship('User', foreign_keys=[submitted_by])

    def is_effectively_active(self):
        """订阅驱动：上架且有人订阅才抓取"""
        return bool(self.enabled) and (self.subscriber_count or 0) > 0

    departments = db.relationship('Department', backref='school', lazy='dynamic',
                                  cascade='all, delete-orphan')
    announcements = db.relationship('Announcement', backref='school', lazy='dynamic',
                                    cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'url': self.url,
            'enabled': self.enabled,
            'config': self.config,
            'submitted_by': self.submitted_by,
            'subscriber_count': self.subscriber_count or 0,
            'active': self.is_effectively_active(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'dept_count': self.departments.count(),
            'announcement_count': self.announcements.count(),
        }


class Department(db.Model):
    """部门/分类"""
    __tablename__ = 'departments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False, comment='部门名称')
    list_url = db.Column(db.String(1000), comment='通知列表页URL')
    list_selector = db.Column(db.String(500), comment='列表项CSS选择器')
    title_selector = db.Column(db.String(500), comment='标题CSS选择器')
    link_selector = db.Column(db.String(500), comment='链接CSS选择器')
    date_selector = db.Column(db.String(500), comment='日期CSS选择器')
    content_selector = db.Column(db.String(500), comment='正文内容CSS选择器')
    group_name = db.Column(db.String(200), comment='导航分组名称（如院系设置、科学研究）')
    last_scraped_at = db.Column(db.DateTime, comment='上次完整抓取完成时间（用于增量抓取）')

    announcements = db.relationship('Announcement', backref='department', lazy='dynamic',
                                    cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'school_id': self.school_id,
            'name': self.name,
            'list_url': self.list_url,
            'list_selector': self.list_selector,
            'title_selector': self.title_selector,
            'link_selector': self.link_selector,
            'date_selector': self.date_selector,
            'content_selector': self.content_selector,
            'group_name': self.group_name,
            'last_scraped_at': self.last_scraped_at.isoformat() if self.last_scraped_at else None,
        }


class Announcement(db.Model):
    """通知/公告"""
    __tablename__ = 'announcements'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False)
    title = db.Column(db.String(500), nullable=False, comment='标题')
    url = db.Column(db.String(2000), comment='原文链接')
    content_html = db.Column(db.Text, comment='正文HTML')
    content_text = db.Column(db.Text, comment='正文纯文本')
    summary = db.Column(db.Text, comment='AI摘要')
    published_at = db.Column(db.DateTime, comment='发布时间')
    content_hash = db.Column(db.String(64), comment='内容哈希（用于变更检测）')
    is_updated = db.Column(db.Boolean, default=False, comment='是否被更新过')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.Index('idx_announcement_hash', 'content_hash'),
        db.Index('idx_announcement_url', 'url'),
        db.Index('idx_announcement_school_dept', 'school_id', 'department_id'),
        db.Index('idx_announcement_published', 'published_at'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'school_id': self.school_id,
            'department_id': self.department_id,
            'title': self.title,
            'url': self.url,
            'content_html': self.content_html,
            'content_text': self.content_text,
            'summary': self.summary,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'content_hash': self.content_hash,
            'is_updated': self.is_updated,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class ScrapeLog(db.Model):
    """爬取日志"""
    __tablename__ = 'scrape_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=True)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime)
    new_count = db.Column(db.Integer, default=0, comment='新增数量')
    total_count = db.Column(db.Integer, default=0, comment='检查总数')
    status = db.Column(db.String(20), default='running', comment='running/success/failed')
    error_message = db.Column(db.Text)

    def to_dict(self):
        return {
            'id': self.id,
            'school_id': self.school_id,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'new_count': self.new_count,
            'total_count': self.total_count,
            'status': self.status,
            'error_message': self.error_message,
        }


class User(db.Model):
    """用户（多用户化）"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username = db.Column(db.String(32), nullable=False, unique=True, comment='用户名')
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(10), nullable=False, default='user', comment='admin/user')
    security_question = db.Column(db.String(200), comment='密保问题')
    security_answer_hash = db.Column(db.String(64), comment='密保答案哈希')
    recovery_attempts = db.Column(db.Integer, default=0, comment='找回尝试计数')
    recovery_locked_until = db.Column(db.DateTime, comment='找回锁定截止时间')
    last_login_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    subscriptions = db.relationship('Subscription', backref='user',
                                    cascade='all, delete-orphan', lazy='dynamic')
    reads = db.relationship('UserRead', backref='user',
                            cascade='all, delete-orphan', lazy='dynamic')

    @property
    def is_admin(self):
        return self.role == 'admin'

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'last_login_at': self.last_login_at.isoformat() if self.last_login_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class Subscription(db.Model):
    """用户订阅学校（订阅驱动抓取）"""
    __tablename__ = 'subscriptions'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    school = db.relationship('School', backref=db.backref(
        'subscriptions', cascade='all, delete-orphan', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'school_id', name='uq_subscription_user_school'),
        db.Index('idx_subscriptions_school', 'school_id'),
    )


class UserRead(db.Model):
    """每用户已读状态（替代 announcements.is_read 全局位）"""
    __tablename__ = 'user_reads'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    announcement_id = db.Column(db.Integer, db.ForeignKey(
        'announcements.id', ondelete='CASCADE'), nullable=False)
    read_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    announcement = db.relationship('Announcement', backref=db.backref(
        'reads', cascade='all, delete-orphan', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('user_id', 'announcement_id', name='uq_user_read'),
        db.Index('idx_user_reads_user', 'user_id'),
    )


class RateBucket(db.Model):
    """通用限流计数（落库，跨 worker 共享）"""
    __tablename__ = 'rate_buckets'

    key = db.Column(db.String(150), primary_key=True,
                    comment='login:ip / login:user / register:ip / submit:user 等')
    count = db.Column(db.Integer, nullable=False, default=0)
    window_start = db.Column(db.DateTime, nullable=False)


class AppConfig(db.Model):
    """应用配置键值存储"""
    __tablename__ = 'app_config'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)

    @staticmethod
    def get(key, default=None):
        item = AppConfig.query.filter_by(key=key).first()
        return item.value if item else default

    @staticmethod
    def set(key, value):
        item = AppConfig.query.filter_by(key=key).first()
        if item:
            item.value = value
        else:
            item = AppConfig(key=key, value=value)
            db.session.add(item)
        db.session.commit()
