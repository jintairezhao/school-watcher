"""发现进度追踪

提供站点发现过程的实时进度推送，支持 SSE (Server-Sent Events)。
用于前端发现向导的实时状态更新。
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


class DiscoverySession:
    """单次站点发现会话的进度追踪器。"""

    def __init__(self, session_id, school_id, school_name):
        self.session_id = session_id
        self.school_id = school_id
        self.school_name = school_name
        self.status = 'running'  # running | completed | failed | cancelled
        self.current_phase = 'started'
        self.progress_pct = 0
        self.events = []  # 保留事件历史用于轮询回退
        self.message_queue = queue.Queue()  # SSE 消息队列
        self.started_at = datetime.now(timezone.utc)
        self.completed_at = None
        self.result = None  # 完成后的发现结果
        self.error = None

        # 统计
        self.total_departments = 0
        self.processed_departments = 0
        self.found_departments = []
        self.skipped_departments = []

    def emit(self, data):
        """发送 SSE 事件。"""
        event = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            **data,
        }
        self.events.append(event)
        self.message_queue.put(json.dumps(event, ensure_ascii=False))

    def set_phase(self, phase, message='', progress_pct=None, **extra):
        """更新当前阶段。"""
        self.current_phase = phase
        if progress_pct is not None:
            self.progress_pct = progress_pct
        self.emit({
            'type': 'phase',
            'phase': phase,
            'message': message,
            'progress_pct': self.progress_pct,
            **extra,
        })

    def add_found(self, dept_info):
        """记录一个发现的部门。"""
        self.found_departments.append(dept_info)
        self.processed_departments += 1
        self.emit({
            'type': 'dept_found',
            'dept': dept_info,
            'processed': self.processed_departments,
            'total': self.total_departments,
        })

    def add_skipped(self, name, reason):
        """记录一个跳过的链接。"""
        self.skipped_departments.append({'name': name, 'reason': reason})
        self.processed_departments += 1
        self.emit({
            'type': 'dept_skipped',
            'name': name,
            'reason': reason,
        })

    def complete(self, result=None):
        """标记完成。"""
        self.status = 'completed'
        self.completed_at = datetime.now(timezone.utc)
        self.progress_pct = 100
        self.result = result
        self.emit({
            'type': 'completed',
            'message': f'发现完成：{len(self.found_departments)} 个候选部门',
            'result': result,
        })
        # 发送完成信号
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

    def cancel(self):
        """取消。"""
        self.status = 'cancelled'
        self.completed_at = datetime.now(timezone.utc)
        self.emit({
            'type': 'cancelled',
            'message': '发现已取消',
        })
        self.message_queue.put('__DONE__')

    def events_generator(self):
        """SSE 事件生成器。"""
        # 先发送历史事件
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
                # 发送心跳
                yield f": heartbeat\n\n"
                if self.status in ('completed', 'failed', 'cancelled'):
                    break

    def to_dict(self):
        """转为可序列化的摘要。"""
        return {
            'session_id': self.session_id,
            'school_id': self.school_id,
            'school_name': self.school_name,
            'status': self.status,
            'current_phase': self.current_phase,
            'progress_pct': self.progress_pct,
            'total_departments': self.total_departments,
            'processed_departments': self.processed_departments,
            'found_count': len(self.found_departments),
            'skipped_count': len(self.skipped_departments),
            'started_at': self.started_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'error': self.error,
            'found_departments': self.found_departments[:10],  # 预览前10个
            'skipped_departments': self.skipped_departments[:10],
        }


def create_session(school_id, school_name):
    """创建新的发现会话。"""
    session_id = f"discovery_{school_id}_{int(time.time() * 1000)}"
    session = DiscoverySession(session_id, school_id, school_name)
    with _sessions_lock:
        _sessions[session_id] = session
        # 清理旧会话（同学校的）
        to_delete = [
            sid for sid, s in _sessions.items()
            if s.school_id == school_id and sid != session_id
        ]
        for sid in to_delete:
            del _sessions[sid]
    return session


def get_session(session_id):
    """获取会话。"""
    with _sessions_lock:
        return _sessions.get(session_id)


def get_school_session(school_id):
    """获取学校的最新会话。"""
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
        logger.info(f"Cleaned up {len(to_delete)} old discovery sessions")
