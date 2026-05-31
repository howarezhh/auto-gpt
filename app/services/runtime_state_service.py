from __future__ import annotations

from threading import Lock


class RuntimeStateService:
    """维护进程内运行时请求计数。"""

    _lock = Lock()
    _active_requests = 0
    _peak_active_requests = 0

    @classmethod
    def enter_request(cls) -> int:
        """进入请求处理时增加活动请求数。"""
        with cls._lock:
            cls._active_requests += 1
            if cls._active_requests > cls._peak_active_requests:
                cls._peak_active_requests = cls._active_requests
            return cls._active_requests

    @classmethod
    def leave_request(cls) -> int:
        """离开请求处理时减少活动请求数。"""
        with cls._lock:
            cls._active_requests = max(0, cls._active_requests - 1)
            return cls._active_requests

    @classmethod
    def current_active_requests(cls) -> int:
        """返回当前活动请求数。"""
        with cls._lock:
            return cls._active_requests

    @classmethod
    def peak_active_requests(cls) -> int:
        """返回历史峰值活动请求数。"""
        with cls._lock:
            return cls._peak_active_requests
