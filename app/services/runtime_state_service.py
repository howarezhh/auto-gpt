from __future__ import annotations

from threading import Lock


class RuntimeStateService:
    _lock = Lock()
    _active_requests = 0
    _peak_active_requests = 0

    @classmethod
    def enter_request(cls) -> int:
        with cls._lock:
            cls._active_requests += 1
            if cls._active_requests > cls._peak_active_requests:
                cls._peak_active_requests = cls._active_requests
            return cls._active_requests

    @classmethod
    def leave_request(cls) -> int:
        with cls._lock:
            cls._active_requests = max(0, cls._active_requests - 1)
            return cls._active_requests

    @classmethod
    def current_active_requests(cls) -> int:
        with cls._lock:
            return cls._active_requests

    @classmethod
    def peak_active_requests(cls) -> int:
        with cls._lock:
            return cls._peak_active_requests
