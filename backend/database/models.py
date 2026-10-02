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
        from backend.services.read_state import total_count_by_school
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
            'announcement_count': total_count_by_school([self.id]).get(self.id, 0),
        }


class Department(db.Model):
    """部门/分类"""
    __tablename__ = 'departments'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False, comment='部门名称')
    kind = db.Column(db.String(16), nullable=False, default='column', server_default='column')
    structure_key = db.Column(db.String(64), unique=True, index=True, comment='官网目录节点的稳定标识')
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
    source_links = db.relationship('AnnouncementSource', backref='department',
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


class DepartmentDirectoryEntry(db.Model):
    """An observed official directory membership; a unit may occur in several lists."""
    __tablename__ = 'department_directory_entries'

    parent_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True)
    position = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.Index('ix_directory_entry_department', 'department_id'),)


class Announcement(db.Model):
    """通知/公告"""
    __tablename__ = 'announcements'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id'), nullable=False)
    title = db.Column(db.String(500), nullable=False, comment='标题')
    url = db.Column(db.String(2000), comment='原文链接')
    canonical_url = db.Column(db.Text, comment='Normalized original address, preserving application routes')
    url_key = db.Column(db.String(64), comment='SHA-256 of canonical_url; NULL for unlinked history')
    content_html = db.Column(db.Text, comment='正文HTML')
    content_text = db.Column(db.Text, comment='正文纯文本')
    content_cached_at = db.Column(db.DateTime)
    content_accessed_at = db.Column(db.DateTime)
    content_bytes = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    content_error = db.Column(db.String(300), nullable=False, default='', server_default='')
    summary = db.Column(db.Text, comment='AI摘要')
    published_at = db.Column(db.DateTime, comment='发布时间')
    content_hash = db.Column(db.String(64), comment='内容哈希（用于变更检测）')
    is_updated = db.Column(db.Boolean, default=False, comment='是否被更新过')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    source_links = db.relationship('AnnouncementSource', backref='announcement',
                                   cascade='all, delete-orphan')

    __table_args__ = (
        db.Index('uq_announcement_school_url_key', 'school_id', 'url_key', unique=True),
        db.Index('ix_content_accessed', 'content_accessed_at'),
        db.Index('idx_announcement_hash', 'content_hash'),
        db.Index('idx_announcement_url', 'url'),
        db.Index('idx_announcement_school_dept', 'school_id', 'department_id'),
        db.Index('idx_announcement_published', 'published_at'),
    )

    def to_dict(self):
        from backend.services.summaries import current_summary
        return {
            'id': self.id,
            'school_id': self.school_id,
            'department_id': self.department_id,
            'title': self.title,
            'url': self.url,
            'content_html': self.content_html,
            'content_text': self.content_text,
            'summary': current_summary(self),
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'content_hash': self.content_hash,
            'is_updated': self.is_updated,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class AnnouncementMerge(db.Model):
    """Durable mapping retained when historical duplicate article IDs are merged."""
    __tablename__ = 'announcement_merges'
    old_id = db.Column(db.Integer, primary_key=True)
    survivor_id = db.Column(db.Integer, nullable=False, index=True)
    merged_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AnnouncementSource(db.Model):
    """Observed appearances of one original article in official publication columns."""
    __tablename__ = 'announcement_sources'

    announcement_id = db.Column(db.Integer, db.ForeignKey('announcements.id', ondelete='CASCADE'), primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True)
    list_url = db.Column(db.String(1000), nullable=False, server_default='')
    article_url = db.Column(db.String(2000), nullable=False, server_default='')
    first_seen_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    last_seen_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.Index('idx_announcement_source_department', 'department_id', 'announcement_id'),)


class ScrapeLog(db.Model):
    """爬取日志"""
    __tablename__ = 'scrape_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    school_id = db.Column(db.Integer, db.ForeignKey('schools.id'), nullable=True)
    source_name = db.Column(db.String(200), nullable=False, default='', server_default='')
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime)
    new_count = db.Column(db.Integer, default=0, comment='新增数量')
    total_count = db.Column(db.Integer, default=0, comment='检查总数')
    status = db.Column(db.String(20), default='running', comment='running/success/failed')
    error_message = db.Column(db.Text)
    task_id = db.Column(db.Integer, db.ForeignKey('background_tasks.id', ondelete='SET NULL'))
    task_generation = db.Column(db.Integer)
    task_token = db.Column(db.String(32))
    __table_args__ = (
        db.Index('ix_scrape_logs_started_id', 'started_at', 'id'),
        db.Index('ix_scrape_logs_school_started', 'school_id', 'started_at', 'id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'school_id': self.school_id,
            'source_name': self.source_name,
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
    auth_version = db.Column(db.Integer, nullable=False, default=0, server_default='0')
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
    department_ids = db.Column(db.JSON, nullable=True,
                               comment='NULL 订阅全部栏目；数组只订阅所选栏目')
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


class UserAnnouncementState(db.Model):
    """个人收藏；旧版归档字段仅用于兼容历史数据，通知仍显示在收件箱。"""
    __tablename__ = 'user_announcement_states'

    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True)
    announcement_id = db.Column(db.Integer, db.ForeignKey('announcements.id', ondelete='CASCADE'), primary_key=True)
    starred = db.Column(db.Boolean, nullable=False, default=False, server_default='0')
    # Retain old records and portable backups without changing subscriptions or stars.
    archived = db.Column(db.Boolean, nullable=False, default=False, server_default='0')


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


class BackgroundTask(db.Model):
    __tablename__ = 'background_tasks'
    id = db.Column(db.Integer, primary_key=True)
    identity = db.Column(db.String(240), nullable=False, unique=True)
    kind = db.Column(db.String(40), nullable=False)
    payload = db.Column(db.JSON, nullable=False, default=dict)
    result = db.Column(db.JSON, nullable=False, default=dict)
    state = db.Column(db.String(16), nullable=False, default='pending')
    attempts = db.Column(db.Integer, nullable=False, default=0)
    capability = db.Column(db.String(16), nullable=False, default='http', server_default='http')
    phase = db.Column(db.String(32), nullable=False, default='fetch', server_default='fetch')
    generation = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    policy_version = db.Column(db.String(64), nullable=False, default='1', server_default='1')
    checkpoint = db.Column(db.JSON, nullable=False, default=dict)
    claim_count = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    worker_id = db.Column(db.String(100))
    error_code = db.Column(db.String(64), nullable=False, default='', server_default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    queued_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    checked_at = db.Column(db.DateTime)
    next_run_at = db.Column(db.DateTime)
    deadline_at = db.Column(db.DateTime)
    available_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    lease_until = db.Column(db.DateTime)
    token = db.Column(db.String(32))
    error = db.Column(db.Text, nullable=False, default='')
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)
    __table_args__ = (db.Index('ix_tasks_claim', 'state', 'available_at'),
                     db.Index('ix_tasks_capability_claim', 'capability', 'state', 'available_at'))


class RuntimeLease(db.Model):
    """Cross-process scheduler, source and directory ownership."""
    __tablename__ = 'runtime_leases'
    key = db.Column(db.String(240), primary_key=True)
    token = db.Column(db.String(64), nullable=False)
    owner_id = db.Column(db.String(100), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class WorkerHeartbeat(db.Model):
    __tablename__ = 'worker_heartbeats'
    worker_id = db.Column(db.String(100), primary_key=True)
    roles = db.Column(db.JSON, nullable=False, default=list)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    heartbeat_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    stopped_at = db.Column(db.DateTime)


class OriginBudget(db.Model):
    """One row serializes reservations for all transports on an official host."""
    __tablename__ = 'origin_budgets'
    origin = db.Column(db.String(512), primary_key=True)
    next_start_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    cooldown_until = db.Column(db.DateTime)


class OriginPermit(db.Model):
    __tablename__ = 'origin_permits'
    token = db.Column(db.String(64), primary_key=True)
    origin = db.Column(db.String(512), nullable=False, index=True)
    owner_id = db.Column(db.String(100), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)


class VerificationSession(db.Model):
    __tablename__ = 'verification_sessions'
    id = db.Column(db.String(64), primary_key=True)
    source_id = db.Column(db.String(128), nullable=False, index=True)
    origin = db.Column(db.String(512), nullable=False, index=True)
    url = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(32), nullable=False, default='required')
    runtime_id = db.Column(db.String(64))
    generation = db.Column(db.String(64))
    task_id = db.Column(db.Integer, db.ForeignKey('background_tasks.id', ondelete='SET NULL'))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    error_code = db.Column(db.String(64), nullable=False, default='', server_default='')
    error_message = db.Column(db.Text, nullable=False, default='', server_default='')
    request_payload = db.Column(db.JSON)


class VerificationWaiter(db.Model):
    __tablename__ = 'verification_waiters'
    task_id = db.Column(db.Integer, db.ForeignKey('background_tasks.id', ondelete='CASCADE'), primary_key=True)
    session_id = db.Column(db.String(64), db.ForeignKey('verification_sessions.id', ondelete='CASCADE'), nullable=False, index=True)


class CatalogPublication(db.Model):
    """Only a fenced main-database transaction can activate a directory snapshot."""
    __tablename__ = 'catalog_publications'
    site_key = db.Column(db.String(128), primary_key=True)
    generation = db.Column(db.String(64), nullable=False)
    path = db.Column(db.Text, nullable=False)
    activated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    policy_version = db.Column(db.String(64), nullable=False, default='1', server_default='1')
