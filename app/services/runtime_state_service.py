from __future__ import annotations

import asyncio
import time
from collections import deque
from threading import Lock
from typing import Any


class RuntimeStateService:
    """维护进程内运行时请求计数。"""

    _lock = Lock()
    _active_requests = 0
    _peak_active_requests = 0
    _event_loop_task: asyncio.Task | None = None
    _event_loop_interval_seconds = 1.0
    _event_loop_window_seconds = 60.0
    _event_loop_latest_delay_ms: float | None = None
    _event_loop_max_delay_ms: float | None = None
    _event_loop_lifetime_max_delay_ms: float | None = None
    _event_loop_avg_delay_ms: float | None = None
    _event_loop_sample_count = 0
    _event_loop_recent_samples: deque[tuple[float, float]] = deque()

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

    @classmethod
    async def start_event_loop_monitor(cls, *, interval_seconds: float = 1.0) -> None:
        """启动当前 worker 事件循环延迟采样。"""
        interval = max(0.1, float(interval_seconds or 1.0))
        cls._event_loop_interval_seconds = interval
        if cls._event_loop_task is not None and not cls._event_loop_task.done():
            return
        cls._event_loop_task = asyncio.create_task(
            cls._event_loop_monitor(interval),
            name="runtime-event-loop-lag-monitor",
        )

    @classmethod
    async def stop_event_loop_monitor(cls) -> None:
        """停止事件循环延迟采样任务。"""
        task = cls._event_loop_task
        cls._event_loop_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @classmethod
    async def _event_loop_monitor(cls, interval_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time() + interval_seconds
        while True:
            await asyncio.sleep(interval_seconds)
            now = loop.time()
            delay_ms = max(0.0, (now - expected) * 1000)
            cls._record_event_loop_delay(delay_ms, sampled_at=now)
            expected = now + interval_seconds

    @classmethod
    def _record_event_loop_delay(cls, delay_ms: float, *, sampled_at: float | None = None) -> None:
        sample_time = sampled_at if sampled_at is not None else time.monotonic()
        with cls._lock:
            cls._event_loop_latest_delay_ms = delay_ms
            cls._event_loop_lifetime_max_delay_ms = (
                delay_ms
                if cls._event_loop_lifetime_max_delay_ms is None
                else max(cls._event_loop_lifetime_max_delay_ms, delay_ms)
            )
            cls._event_loop_sample_count += 1
            cls._event_loop_recent_samples.append((sample_time, delay_ms))
            cls._trim_event_loop_samples_locked(sample_time)
            cls._refresh_event_loop_window_stats_locked()

    @classmethod
    def event_loop_snapshot(cls) -> dict[str, Any]:
        """返回当前 worker 的事件循环延迟采样快照。"""
        with cls._lock:
            cls._trim_event_loop_samples_locked(time.monotonic())
            cls._refresh_event_loop_window_stats_locked()
            latest = cls._event_loop_latest_delay_ms
            maximum = cls._event_loop_max_delay_ms
            lifetime_maximum = cls._event_loop_lifetime_max_delay_ms
            average = cls._event_loop_avg_delay_ms
            return {
                "scope": "single_worker",
                "sample_count": cls._event_loop_sample_count,
                "recent_sample_count": len(cls._event_loop_recent_samples),
                "interval_seconds": cls._event_loop_interval_seconds,
                "window_seconds": int(cls._event_loop_window_seconds),
                "latest_delay_ms": round(latest, 2) if latest is not None else None,
                "max_delay_ms": round(maximum, 2) if maximum is not None else None,
                "recent_max_delay_ms": round(maximum, 2) if maximum is not None else None,
                "avg_delay_ms": round(average, 2) if average is not None else None,
                "lifetime_max_delay_ms": round(lifetime_maximum, 2) if lifetime_maximum is not None else None,
            }

    @classmethod
    def _trim_event_loop_samples_locked(cls, now: float) -> None:
        cutoff = now - cls._event_loop_window_seconds
        while cls._event_loop_recent_samples and cls._event_loop_recent_samples[0][0] < cutoff:
            cls._event_loop_recent_samples.popleft()

    @classmethod
    def _refresh_event_loop_window_stats_locked(cls) -> None:
        if not cls._event_loop_recent_samples:
            cls._event_loop_max_delay_ms = None
            cls._event_loop_avg_delay_ms = None
            return
        delays = [delay_ms for _, delay_ms in cls._event_loop_recent_samples]
        cls._event_loop_max_delay_ms = max(delays)
        cls._event_loop_avg_delay_ms = sum(delays) / len(delays)
