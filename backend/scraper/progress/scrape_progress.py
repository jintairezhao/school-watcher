"""抓取进度追踪

提供常规抓取过程的实时进度推送，支持 SSE (Server-Sent Events)。
参考 discovery_progress.py 的 DiscoverySession 模式设计。
用于前端「刷新」按钮的实时状态更新。
"""

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 全局进度会话存储
_sessions = {}
_sessions_lock = threading.Lock()


class ScrapeSession:
    """单次抓取的进度追踪器。"""

    def __init__(self, session_id, school_id, school_name):
        self.session_id = session_id
        self.school_id = school_id
        self.school_name = school_name
        self.status = 'running'  # running | completed | failed
        self.events = []         # 保留事件历史用于轮询回退和延迟连接
        self.message_queue = queue.Queue()
        self.started_at = datetime.now(timezone.utc)
        self.completed_at = None
        self.error = None

        # 统计
        self.total_departments = 0
        self.current_department = ''
        self.current_department_index = 0
        self.new_count = 0
        self.total_processed = 0
        self.department_results = []   # [{name, new, total}]

    def emit(self, data):
        """发送 SSE 事件。"""
        event = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            **data,
        }
        self.events.append(event)
        self.message_queue.put(json.dumps(event, ensure_ascii=False))

    def set_departments(self, departments):
        """设置要爬取的部门列表。departments: list of Department ORM objects."""
        names = [d.name for d in departments]
        self.total_departments = len(names)
        self.emit({
            'type': 'init',
            'total_departments': len(names),
            'departments': names,
        })

    def dept_start(self, dept_name, index):
        """开始爬取某个部门。"""
        self.current_department = dept_name
        self.current_department_index = index
        self.emit({
            'type': 'dept_start',
            'dept': dept_name,
            'index': index,
            'total': self.total_departments,
        })

    def dept_done(self, dept_name, new_count, total):
        """某个部门爬取完成。"""
        self.new_count += new_count
        self.total_processed += total
        self.department_results.append({
            'name': dept_name,
            'new': new_count,
            'total': total,
        })
        self.emit({
            'type': 'dept_done',
            'dept': dept_name,
            'new': new_count,
            'subtotal': total,
            'cumulative_new': self.new_count,
            'progress_pct': (
                round(self.current_department_index / self.total_departments * 100)
                if self.total_departments > 0 else 0
            ),
        })

    def complete(self, new_count=None, message=None):
        """标记完成。"""
        self.status = 'completed'
        self.completed_at = datetime.now(timezone.utc)
        if new_count is not None:
            self.new_count = new_count
        if message is None:
            message = f'爬取完成，新增 {self.new_count} 条通知'
        self.emit({
            'type': 'completed',
            'new_count': self.new_count,
            'total_processed': self.total_processed,
            'total_departments': self.total_departments,
            'message': message,
        })
        self.message_queue.put('__DONE__')

    def fail(self, error_message):
        """标记失败。"""
        self.status = 'failed'
        self.completed_at = datetime.now(timezone.utc)
        self.error = error_message
        self.emit({
            'type': 'failed',
            'message': error_message,
        })
        self.message_queue.put('__DONE__')

    def events_generator(self):
        """SSE 事件生成器（Flask Response 用）。"""
        # 先发送历史事件（延迟连接的客户端也能看到已有进度）
        for event in self.events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        # 再监听实时事件
        while True:
            try:
                msg = self.message_queue.get(timeout=30)
                if msg == '__DONE__':
                    yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                # 发送心跳保持连接
                yield f": heartbeat\n\n"
                if self.status in ('completed', 'failed'):
                    break

    def to_dict(self):
        """转为可序列化的摘要（用于轮询回退）。"""
        return {
            'session_id': self.session_id,
            'school_id': self.school_id,
            'school_name': self.school_name,
            'status': self.status,
            'total_departments': self.total_departments,
            'current_department': self.current_department,
            'current_department_index': self.current_department_index,
            'new_count': self.new_count,
            'total_processed': self.total_processed,
            'started_at': self.started_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error': self.error,
            'department_results': self.department_results,
        }


# --- 全局会话管理 ---

def create_session(school_id, school_name):
    """创建新的抓取会话，自动清理同学校的旧会话。"""
    session_id = f"scrape_{school_id}_{int(time.time() * 1000)}"
    session = ScrapeSession(session_id, school_id, school_name)
    with _sessions_lock:
        _sessions[session_id] = session
        # 清理同学校的旧会话
        to_delete = [
            sid for sid, s in _sessions.items()
            if s.school_id == school_id and sid != session_id
        ]
        for sid in to_delete:
            del _sessions[sid]
    logger.info(f"创建抓取会话: {session_id} ({school_name})")
    return session


def get_session(session_id):
    """按 session_id 获取会话。"""
    with _sessions_lock:
        return _sessions.get(session_id)


def get_school_session(school_id):
    """获取学校的最新活跃会话。"""
    with _sessions_lock:
        for sid, s in sorted(_sessions.items(), reverse=True):
            if s.school_id == school_id:
                return s
    return None


def get_all_sessions():
    """获取所有活跃会话。"""
    with _sessions_lock:
        return list(_sessions.values())


def cleanup_old_sessions(max_age_seconds=3600):
    """清理过期会话。"""
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
    with _sessions_lock:
        to_delete = [
            sid for sid, s in _sessions.items()
            if s.started_at.timestamp() < cutoff
        ]
        for sid in to_delete:
            del _sessions[sid]
    if to_delete:
        logger.info(f"清理了 {len(to_delete)} 个过期抓取会话")
