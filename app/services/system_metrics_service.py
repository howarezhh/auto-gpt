from __future__ import annotations

import os
import platform
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - deployment fallback
    psutil = None

from redis import Redis
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import engine
from app.models.alert_event import AlertEvent
from app.models.logging_events import BackgroundJobEvent
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.scheduler import scheduler
from app.services.log_service import LogService
from app.logging.queue import LoggingQueue
from app.services.cache_service import CacheService
from app.services.request_log_queue_service import RequestLogQueueService
from app.services.provider_capacity_service import ProviderCapacityService, ProviderCapacityUnavailableError
from app.services.provider_service import ProviderService
from app.services.runtime_state_service import RuntimeStateService
from app.services.setting_service import SettingService
from app.utils.json_utils import dumps_json


PROCESS_STARTED_AT = time.time()


class SystemMetricsService:
    TRAFFIC_LOG_TYPES = ("chat", "responses", "moderations", "files")
    ACTIVE_REQUEST_WARNING_THRESHOLD = 900
    ACTIVE_STREAM_WARNING_THRESHOLD = 300
    ERROR_RATE_5XX_WARNING_THRESHOLD = 1.0
    ERROR_RATE_429_WARNING_THRESHOLD = 5.0
    ERROR_COUNT_429_WARNING_THRESHOLD = 5
    PROVIDER_FAILURE_RATE_WARNING_THRESHOLD = 20.0
    BACKGROUND_BACKLOG_WARNING_THRESHOLD = 1000
    BILLING_FAILURE_WARNING_THRESHOLD = 10
    TOKEN_FAILURE_WARNING_THRESHOLD = 10
    CONTENT_GUARD_HIGH_RISK_PROVIDER_THRESHOLD = 3
    CONTENT_GUARD_HIGH_RISK_PROVIDER_WINDOW_MINUTES = 10
    CONTENT_GUARD_HIGH_RISK_PROVIDER_ALERT_PREFIX = "monitoring:content_guard_high_risk_provider:"
    METRIC_PERCENTILE_SAMPLE_LIMIT = 5000
    PROJECT_PROCESS_CACHE_SECONDS = 30
    _project_process_cache: dict[str, Any] = {
        "path": None,
        "snapshot": None,
        "checked_at": 0.0,
        "cpu_times": {},
        "cpu_checked_at": 0.0,
    }

    @classmethod
    def collect(
        cls,
        db: Session,
        *,
        window_minutes: int = 5,
        refresh_alerts: bool = False,
    ) -> dict[str, Any]:
        window_minutes = max(1, min(int(window_minutes or 5), 1440))
        database = cls._database_snapshot(db)
        limits = cls._limits_snapshot(db) if database["ok"] else cls._limits_snapshot(None)
        redis_snapshot = cls._redis_snapshot()
        redis_snapshot.update(limits)
        runtime = cls._runtime_snapshot()
        host = cls._host_snapshot()
        section_errors: dict[str, str] = {}
        traffic = (
            cls._safe_snapshot(
                db,
                section_errors,
                "traffic",
                lambda: cls._traffic_snapshot(db, window_minutes=window_minutes),
                cls._empty_traffic,
            )
            if database["ok"]
            else cls._empty_traffic()
        )
        bucket_minutes = cls._bucket_minutes(window_minutes)
        timeseries = (
            cls._safe_snapshot(
                db,
                section_errors,
                "timeseries",
                lambda: LogService.metric_timeseries(db, window_minutes=window_minutes, bucket_minutes=bucket_minutes),
                list,
            )
            if database["ok"]
            else []
        )
        providers = (
            cls._safe_snapshot(
                db,
                section_errors,
                "providers",
                lambda: cls._provider_snapshot(db, window_minutes=window_minutes),
                list,
            )
            if database["ok"]
            else []
        )
        content_guard = (
            cls._safe_snapshot(
                db,
                section_errors,
                "content_guard",
                lambda: cls._content_guard_snapshot(db, window_minutes=window_minutes),
                cls._empty_content_guard,
            )
            if database["ok"]
            else cls._empty_content_guard()
        )
        background = (
            cls._safe_snapshot(
                db,
                section_errors,
                "background",
                lambda: cls._background_snapshot(db),
                cls._empty_background,
            )
            if database["ok"]
            else cls._empty_background()
        )
        pool = cls._database_pool_snapshot()
        status = cls._resolve_status(
            database_ok=database["ok"],
            redis_ok=redis_snapshot["ok"],
            background=background,
            traffic=traffic,
        )
        if section_errors and status == "ready":
            status = "degraded"
        metrics = {
            "status": status,
            "window_minutes": window_minutes,
            "generated_at": datetime.utcnow().isoformat(),
            "database": {**database, "pool": pool},
            "redis": redis_snapshot,
            "runtime": runtime,
            "cache": CacheService.stats_snapshot(),
            "host": host,
            "traffic": traffic,
            "timeseries": timeseries,
            "bucket_minutes": bucket_minutes,
            "providers": providers,
            "content_guard": content_guard,
            "background": background,
            "section_errors": section_errors,
        }
        metrics["alerts"] = cls._evaluate_alerts(metrics)
        if database["ok"]:
            cls.apply_monitoring_actions(db, metrics)
            if refresh_alerts:
                cls.write_monitoring_alerts(db, metrics)
        return metrics

    @staticmethod
    def _safe_snapshot(
        db: Session,
        section_errors: dict[str, str],
        section_name: str,
        loader: Any,
        fallback_factory: Any,
    ) -> Any:
        try:
            return loader()
        except Exception as exc:
            if db is not None:
                db.rollback()
            section_errors[section_name] = str(exc)
            return fallback_factory()

    @classmethod
    def write_monitoring_alerts(cls, db: Session, metrics: dict[str, Any]) -> None:
        active_events = cls._build_monitoring_alert_events(metrics)
        changed = cls.apply_monitoring_alert_actions(db, active_events, auto_commit=False)
        existing_items = list(
            db.scalars(
                select(AlertEvent).where(
                    AlertEvent.alert_key.like("monitoring:%")
                )
            )
        )
        existing_by_key = {item.alert_key: item for item in existing_items}
        active_keys = set(active_events)
        now = datetime.utcnow()
        for alert_key, payload in active_events.items():
            item = existing_by_key.get(alert_key)
            if item is None:
                item = AlertEvent(
                    alert_key=alert_key,
                    alert_type=payload["alert_type"],
                    first_seen_at=now,
                )
                db.add(item)
            item.severity = payload["severity"]
            item.title = payload["title"]
            item.message = payload["message"]
            item.payload_json = dumps_json(payload["payload"])
            item.status = "active"
            item.last_seen_at = now
            item.resolved_at = None
            changed = True
        for item in existing_items:
            if item.alert_key in active_keys or item.status == "resolved":
                continue
            item.status = "resolved"
            item.resolved_at = now
            changed = True
        if changed:
            db.commit()

    @classmethod
    def apply_monitoring_actions(cls, db: Session, metrics: dict[str, Any]) -> bool:
        active_events = cls._build_monitoring_alert_events(metrics)
        return cls.apply_monitoring_alert_actions(db, active_events)

    @classmethod
    def apply_monitoring_alert_actions(
        cls,
        db: Session,
        active_events: dict[str, dict[str, Any]],
        *,
        auto_commit: bool = True,
    ) -> bool:
        changed = cls._apply_monitoring_actions(db, active_events, now=datetime.utcnow())
        if changed and auto_commit:
            db.commit()
        return changed

    @classmethod
    def _apply_monitoring_actions(
        cls,
        db: Session,
        active_events: dict[str, dict[str, Any]],
        *,
        now: datetime,
    ) -> bool:
        changed = False
        for alert_key, event in active_events.items():
            if not alert_key.startswith(cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_ALERT_PREFIX):
                continue
            try:
                changed = cls._auto_isolate_high_risk_content_provider(db, event, now=now) or changed
            except Exception as exc:
                payload = event.setdefault("payload", {})
                if isinstance(payload, dict):
                    payload["auto_isolated"] = False
                    payload["isolation_failed"] = True
                    payload["isolation_status"] = "failed"
                    payload["isolation_error"] = str(exc)
                event["message"] = f"{event.get('message') or '内容高风险自动隔离'}；自动隔离失败：{exc}"
        if changed:
            ProviderService.invalidate_provider_runtime_cache()
        return changed

    @classmethod
    def _auto_isolate_high_risk_content_provider(
        cls,
        db: Session,
        event: dict[str, Any],
        *,
        now: datetime,
    ) -> bool:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return False
        provider_id = payload.get("provider_id")
        try:
            provider_id = int(provider_id)
        except (TypeError, ValueError):
            return False
        high_risk_count = int(payload.get("high_risk_count") or 0)
        if high_risk_count < cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_THRESHOLD:
            return False
        provider = db.get(Provider, provider_id)
        if provider is None:
            return False

        changed = False
        is_new_isolation = (
            provider.trust_level != "blocked"
            or provider.content_integrity_status != "blocked"
            or provider.circuit_state != "open"
        )
        updates = {
            "trust_level": "blocked",
            "content_integrity_status": "blocked",
            "circuit_state": "open",
            "low_trust_route_enabled": False,
        }
        for field, value in updates.items():
            if getattr(provider, field, None) == value:
                continue
            setattr(provider, field, value)
            changed = True

        new_score = max(0, min(int(provider.content_integrity_score or 80), 20))
        if provider.content_integrity_score != new_score:
            provider.content_integrity_score = new_score
            changed = True
        if is_new_isolation and provider.last_content_violation_at != now:
            provider.last_content_violation_at = now
            changed = True

        for provider_model in provider.provider_models:
            model_updates = {
                "content_integrity_status": "blocked",
                "circuit_state": "open",
            }
            for field, value in model_updates.items():
                if getattr(provider_model, field) == value:
                    continue
                setattr(provider_model, field, value)
                changed = True
            if is_new_isolation and provider_model.circuit_opened_at != now:
                provider_model.circuit_opened_at = now
                changed = True

        payload["auto_isolated"] = True
        payload["isolation_status"] = "blocked"
        payload["isolated_at"] = now.isoformat()
        event["message"] = (
            f"最近 {payload.get('window_minutes', cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_WINDOW_MINUTES)} 分钟"
            f"内容高风险命中 {high_risk_count} 次，已自动隔离并从路由候选排除"
        )
        return changed

    @staticmethod
    def _database_snapshot(db: Session) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            db.execute(text("SELECT 1")).scalar()
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            return {
                "ok": True,
                "status": "ok",
                "latency_ms": latency_ms,
                "dialect": db.get_bind().dialect.name,
                "error": None,
            }
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            return {
                "ok": False,
                "status": "unavailable",
                "latency_ms": latency_ms,
                "dialect": getattr(getattr(db, "bind", None), "dialect", None).name if getattr(db, "bind", None) else None,
                "error": str(exc),
            }

    @staticmethod
    def _database_pool_snapshot() -> dict[str, Any]:
        pool = engine.pool
        snapshot: dict[str, Any] = {
            "class": pool.__class__.__name__,
            "status": pool.status() if hasattr(pool, "status") else None,
        }
        for name in ("size", "checkedout", "checkedin", "overflow"):
            method = getattr(pool, name, None)
            if not callable(method):
                continue
            try:
                snapshot[name] = method()
            except Exception:
                snapshot[name] = None
        settings = get_settings()
        snapshot["configured_pool_size"] = settings.db_pool_size
        snapshot["configured_max_overflow"] = settings.db_max_overflow
        snapshot["configured_pool_timeout"] = settings.db_pool_timeout
        return snapshot

    @classmethod
    def _redis_snapshot(cls) -> dict[str, Any]:
        settings = get_settings()
        if not settings.redis_url.strip():
            return {
                "ok": False,
                "status": "disabled",
                "latency_ms": None,
                "active_requests": None,
                "active_streams": None,
                "token_finalize_backlog": None,
                "request_log_queue": {"queued": None, "processing": None, "total": None},
                "logging_event_queue": {"queued": None, "processing": None, "total": None},
                "error": "REDIS_URL is empty",
            }
        started = time.perf_counter()
        try:
            client = Redis.from_url(settings.redis_url, decode_responses=True)
            try:
                client.ping()
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                active_values = client.mget([
                    "concurrency:global:active",
                    "concurrency:global:streams",
                ])
                request_log_queue = cls._request_log_queue_snapshot(client)
                logging_event_queue = cls._logging_event_queue_snapshot(client)
                return {
                    "ok": True,
                    "status": "ok",
                    "latency_ms": latency_ms,
                    "active_requests": int(active_values[0] or 0),
                    "active_streams": int(active_values[1] or 0),
                    "token_finalize_backlog": cls._count_redis_keys(client, "token_usage:finalize:dedupe:*", limit=1001),
                    "request_log_queue": request_log_queue,
                    "logging_event_queue": logging_event_queue,
                    "scheduler_jobs": cls._scheduler_job_states(client),
                    "error": None,
                }
            finally:
                client.close()
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            return {
                "ok": False,
                "status": "unavailable",
                "latency_ms": latency_ms,
                "active_requests": None,
                "active_streams": None,
                "token_finalize_backlog": None,
                "request_log_queue": {"queued": None, "processing": None, "total": None},
                "logging_event_queue": {"queued": None, "processing": None, "total": None},
                "error": str(exc),
            }

    @staticmethod
    def _request_log_queue_snapshot(client: Redis) -> dict[str, int | None]:
        try:
            queued = int(client.llen(RequestLogQueueService.QUEUE_KEY) or 0)
            processing = int(client.llen(RequestLogQueueService.PROCESSING_KEY) or 0)
            return {"queued": queued, "processing": processing, "total": queued + processing}
        except Exception:
            return {"queued": None, "processing": None, "total": None}

    @staticmethod
    def _logging_event_queue_snapshot(client: Redis) -> dict[str, int | None]:
        try:
            queued = int(client.llen(LoggingQueue.QUEUE_KEY) or 0)
            processing = int(client.llen(LoggingQueue.PROCESSING_KEY) or 0)
            return {"queued": queued, "processing": processing, "total": queued + processing}
        except Exception:
            return {"queued": None, "processing": None, "total": None}

    @staticmethod
    def _limits_snapshot(db: Session | None) -> dict[str, Any]:
        try:
            setting = SettingService.get_or_create(db) if db is not None else None
        except Exception:
            if db is not None:
                db.rollback()
            setting = None
        settings = get_settings()
        return {
            "max_active_requests": int(getattr(setting, "global_max_active_requests", settings.global_max_active_requests) or 0),
            "max_active_streams": int(getattr(setting, "global_max_active_streams", settings.global_max_active_streams) or 0),
        }

    @staticmethod
    def _bucket_minutes(window_minutes: int) -> int:
        if window_minutes <= 15:
            return 1
        if window_minutes <= 60:
            return 5
        if window_minutes <= 360:
            return 15
        return 60

    @staticmethod
    def _count_redis_keys(client: Redis, pattern: str, *, limit: int) -> int:
        count = 0
        for _ in client.scan_iter(match=pattern, count=100):
            count += 1
            if count >= limit:
                return count
        return count

    @staticmethod
    def _scheduler_job_states(client: Redis) -> dict[str, dict[str, str]]:
        states: dict[str, dict[str, str]] = {}
        for key in client.scan_iter(match="scheduler:job:*:state", count=50):
            job_name = str(key).removeprefix("scheduler:job:").removesuffix(":state")
            raw_state = client.hgetall(key)
            states[job_name] = {str(item_key): str(item_value) for item_key, item_value in raw_state.items()}
        return states

    @staticmethod
    def _runtime_snapshot() -> dict[str, Any]:
        return {
            "worker_active_requests": RuntimeStateService.current_active_requests(),
            "worker_peak_active_requests": RuntimeStateService.peak_active_requests(),
            "scope": "single_worker",
            "note": "RuntimeStateService reflects only the current worker; Redis counters are the global concurrency source.",
        }

    @classmethod
    def _host_snapshot(cls) -> dict[str, Any]:
        project_root = cls._project_root()
        now = time.time()
        snapshot: dict[str, Any] = {
            "available": psutil is not None,
            "provider": "psutil" if psutil is not None else "stdlib",
            "scope": {
                "host": "宿主机或当前运行环境",
                "project_process": "当前项目相关进程组",
            },
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "cpu_percent": None,
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "memory": {
                "total_bytes": None,
                "available_bytes": None,
                "used_bytes": None,
                "percent": None,
            },
            "process": {
                "pid": os.getpid(),
                "cpu_percent": None,
                "memory_rss_bytes": None,
                "memory_vms_bytes": None,
                "memory_percent": None,
                "thread_count": None,
                "open_file_count": None,
                "connection_count": None,
                "started_at": datetime.utcfromtimestamp(PROCESS_STARTED_AT).isoformat(),
                "uptime_seconds": round(now - PROCESS_STARTED_AT, 2),
            },
            "project_process": cls._empty_project_process_snapshot(project_root, now),
            "error": None,
        }
        if psutil is None:
            snapshot["error"] = "psutil is not installed"
            return snapshot
        try:
            memory = psutil.virtual_memory()
            process = psutil.Process(os.getpid())
            with process.oneshot():
                process_memory = process.memory_info()
                open_file_count = None
                connection_count = None
                create_time = process.create_time()
                snapshot["process"].update(
                    {
                        "cpu_percent": round(process.cpu_percent(interval=None), 2),
                        "memory_rss_bytes": process_memory.rss,
                        "memory_vms_bytes": process_memory.vms,
                        "memory_percent": round(process.memory_percent(), 2),
                        "thread_count": process.num_threads(),
                        "open_file_count": open_file_count,
                        "connection_count": connection_count,
                        "started_at": datetime.utcfromtimestamp(create_time).isoformat(),
                        "uptime_seconds": round(now - create_time, 2),
                    }
                )
            snapshot["project_process"] = cls._project_process_snapshot(
                project_root,
                memory_total_bytes=memory.total,
                now=now,
            )
            snapshot.update(
                {
                    "cpu_percent": round(psutil.cpu_percent(interval=None), 2),
                    "memory": {
                        "total_bytes": memory.total,
                        "available_bytes": memory.available,
                        "used_bytes": memory.used,
                        "percent": memory.percent,
                    },
                }
            )
            return snapshot
        except Exception as exc:
            snapshot["available"] = False
            snapshot["error"] = str(exc)
            return snapshot

    @staticmethod
    def _project_root() -> str:
        return str(Path(__file__).resolve().parents[2])

    @staticmethod
    def _empty_project_process_snapshot(project_root: str, now: float) -> dict[str, Any]:
        return {
            "path": project_root,
            "scope": "project_process_group",
            "available": False,
            "process_count": 0,
            "pids": [],
            "names": [],
            "cpu_percent": None,
            "cpu_percent_of_one_core": None,
            "cpu_sample_ready": False,
            "memory_rss_bytes": None,
            "memory_vms_bytes": None,
            "memory_percent": None,
            "thread_count": None,
            "oldest_started_at": None,
            "uptime_seconds": None,
            "cache_seconds": SystemMetricsService.PROJECT_PROCESS_CACHE_SECONDS,
            "cache_age_seconds": 0.0,
            "checked_at": datetime.utcfromtimestamp(now).isoformat(),
        }

    @classmethod
    def _project_process_snapshot(
        cls,
        project_root: str,
        *,
        memory_total_bytes: int,
        now: float,
    ) -> dict[str, Any]:
        cached_path = cls._project_process_cache.get("path")
        cached_snapshot = cls._project_process_cache.get("snapshot")
        checked_at = float(cls._project_process_cache.get("checked_at") or 0)
        if (
            cached_path == project_root
            and isinstance(cached_snapshot, dict)
            and now - checked_at < cls.PROJECT_PROCESS_CACHE_SECONDS
        ):
            snapshot = dict(cached_snapshot)
            snapshot["cache_age_seconds"] = round(now - checked_at, 2)
            return snapshot

        previous_cpu_times = dict(cls._project_process_cache.get("cpu_times") or {})
        previous_checked_at = float(cls._project_process_cache.get("cpu_checked_at") or 0)
        cpu_times: dict[int, float] = {}
        process_items: list[dict[str, Any]] = []
        total_rss = 0
        total_vms = 0
        total_threads = 0
        oldest_create_time: float | None = None

        if psutil is None:
            return cls._empty_project_process_snapshot(project_root, now)

        root_path = Path(project_root).resolve()
        current_pid = os.getpid()
        for proc in cls._related_project_processes(root_path, current_pid):
            try:
                if not cls._is_project_process(proc, root_path, current_pid):
                    continue
                with proc.oneshot():
                    memory_info = proc.memory_info()
                    proc_cpu_times = proc.cpu_times()
                    create_time = proc.create_time()
                    thread_count = proc.num_threads()
                    name = proc.name()
                pid = int(proc.pid)
                proc_cpu_total = float(proc_cpu_times.user + proc_cpu_times.system)
                cpu_times[pid] = proc_cpu_total
                total_rss += int(memory_info.rss)
                total_vms += int(memory_info.vms)
                total_threads += int(thread_count)
                oldest_create_time = create_time if oldest_create_time is None else min(oldest_create_time, create_time)
                process_items.append(
                    {
                        "pid": pid,
                        "name": name,
                        "memory_rss_bytes": int(memory_info.rss),
                        "thread_count": int(thread_count),
                        "started_at": datetime.utcfromtimestamp(create_time).isoformat(),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue

        elapsed = now - previous_checked_at if previous_checked_at > 0 else 0
        cpu_delta = 0.0
        if elapsed > 0:
            for pid, current_cpu_time in cpu_times.items():
                previous_cpu_time = previous_cpu_times.get(pid)
                if previous_cpu_time is None:
                    continue
                cpu_delta += max(0.0, current_cpu_time - float(previous_cpu_time))
        cpu_count = os.cpu_count() or 1
        cpu_sample_ready = elapsed > 0 and bool(previous_cpu_times)
        cpu_percent = round((cpu_delta / elapsed / cpu_count) * 100, 2) if cpu_sample_ready else None
        cpu_percent_of_one_core = round((cpu_delta / elapsed) * 100, 2) if cpu_sample_ready else None
        process_items.sort(key=lambda item: int(item["pid"]))
        names = sorted({str(item["name"]) for item in process_items if item.get("name")})
        snapshot = {
            "path": project_root,
            "scope": "project_process_group",
            "available": True,
            "process_count": len(process_items),
            "pids": [item["pid"] for item in process_items],
            "names": names,
            "cpu_percent": cpu_percent,
            "cpu_percent_of_one_core": cpu_percent_of_one_core,
            "cpu_sample_ready": cpu_sample_ready,
            "memory_rss_bytes": total_rss,
            "memory_vms_bytes": total_vms,
            "memory_percent": round((total_rss / memory_total_bytes) * 100, 2) if memory_total_bytes else None,
            "thread_count": total_threads,
            "oldest_started_at": datetime.utcfromtimestamp(oldest_create_time).isoformat() if oldest_create_time else None,
            "uptime_seconds": round(now - oldest_create_time, 2) if oldest_create_time else None,
            "processes": process_items[:50],
            "cache_seconds": cls.PROJECT_PROCESS_CACHE_SECONDS,
            "cache_age_seconds": 0.0,
            "checked_at": datetime.utcfromtimestamp(now).isoformat(),
        }
        cls._project_process_cache = {
            "path": project_root,
            "snapshot": snapshot,
            "checked_at": now,
            "cpu_times": cpu_times,
            "cpu_checked_at": now,
        }
        return snapshot

    @classmethod
    def _related_project_processes(cls, root_path: Path, current_pid: int) -> list[Any]:
        try:
            current = psutil.Process(current_pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return []
        related: dict[int, Any] = {current_pid: current}
        topmost = current
        try:
            for parent in current.parents():
                if not cls._is_project_process(parent, root_path, current_pid):
                    break
                related[int(parent.pid)] = parent
                topmost = parent
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            pass
        try:
            candidates = [topmost, *topmost.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            candidates = [current]
        for candidate in candidates:
            try:
                if cls._is_project_process(candidate, root_path, current_pid):
                    related[int(candidate.pid)] = candidate
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
        for candidate in psutil.process_iter(["pid", "name"]):
            try:
                pid = int(candidate.pid)
                if pid in related:
                    continue
                name = str((getattr(candidate, "info", {}) or {}).get("name") or "").lower()
                if not any(token in name for token in ("python", "gunicorn", "uvicorn")):
                    continue
                if cls._is_project_process(candidate, root_path, current_pid):
                    related[pid] = candidate
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
        return list(related.values())

    @classmethod
    def _is_project_process(cls, proc: Any, root_path: Path, current_pid: int) -> bool:
        if int(proc.pid) == current_pid:
            return True
        info = getattr(proc, "info", {}) or {}
        try:
            cwd = info.get("cwd") if "cwd" in info else proc.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            cwd = None
        if cwd and cls._path_is_inside(cwd, root_path):
            return True
        try:
            cmdline = info.get("cmdline") if "cmdline" in info else proc.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            cmdline = []
        cmdline = cmdline or []
        root_text = os.path.normcase(str(root_path))
        for arg in cmdline:
            if not arg:
                continue
            normalized_arg = os.path.normcase(str(arg).strip().strip('"'))
            if root_text in normalized_arg or cls._path_is_inside(normalized_arg, root_path):
                return True
        return False

    @staticmethod
    def _path_is_inside(candidate: str, root_path: Path) -> bool:
        try:
            candidate_path = Path(candidate)
            if not candidate_path.is_absolute():
                return False
            candidate_path = candidate_path.resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        try:
            candidate_path.relative_to(root_path)
            return True
        except ValueError:
            return False
    @classmethod
    def _traffic_snapshot(cls, db: Session, *, window_minutes: int) -> dict[str, Any]:
        since = datetime.utcnow() - timedelta(minutes=window_minutes)
        row = db.execute(
            select(
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.success.is_(True), 1), else_=0)).label("success_requests"),
                func.sum(case((RequestLog.status_code == 429, 1), else_=0)).label("status_429"),
                func.sum(case((RequestLog.status_code >= 500, 1), else_=0)).label("status_5xx"),
                func.avg(RequestLog.first_token_latency_ms).label("avg_first_token_latency_ms"),
                func.sum(case((RequestLog.is_stream.is_(True), 1), else_=0)).label("stream_requests"),
                func.sum(case((RequestLog.has_image.is_(True), 1), else_=0)).label("image_requests"),
            ).where(
                RequestLog.created_at >= since,
                RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
            )
        ).one()
        total = int(row.total_requests or 0)
        success = int(row.success_requests or 0)
        failed = total - success
        status_429 = int(row.status_429 or 0)
        status_5xx = int(row.status_5xx or 0)
        window_seconds = max(1, window_minutes * 60)
        latencies = list(
            db.scalars(
                select(RequestLog.latency_ms)
                .where(
                    RequestLog.created_at >= since,
                    RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
                    RequestLog.latency_ms.is_not(None),
                )
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(cls.METRIC_PERCENTILE_SAMPLE_LIMIT)
            )
        )
        return {
            "total_requests": total,
            "success_requests": success,
            "failed_requests": failed,
            "status_429": status_429,
            "status_5xx": status_5xx,
            "failure_rate": round((failed / total) * 100, 2) if total else 0.0,
            "status_429_rate": round((status_429 / total) * 100, 2) if total else 0.0,
            "status_5xx_rate": round((status_5xx / total) * 100, 2) if total else 0.0,
            "qps": round(total / window_seconds, 4),
            "stream_qps": round(int(row.stream_requests or 0) / window_seconds, 4),
            "p50_latency_ms": cls._percentile(latencies, 50),
            "p95_latency_ms": cls._percentile(latencies, 95),
            "p99_latency_ms": cls._percentile(latencies, 99),
            "avg_first_token_latency_ms": round(float(row.avg_first_token_latency_ms), 2)
            if row.avg_first_token_latency_ms is not None
            else None,
            "stream_requests": int(row.stream_requests or 0),
            "image_requests": int(row.image_requests or 0),
        }

    @classmethod
    def _content_guard_snapshot(cls, db: Session, *, window_minutes: int) -> dict[str, Any]:
        setting = SettingService.get_or_create(db)
        if not bool(getattr(setting, "content_guard_enabled", True)):
            return cls._empty_content_guard(enabled=False)
        since = datetime.utcnow() - timedelta(minutes=window_minutes)
        high_risk_since = datetime.utcnow() - timedelta(minutes=cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_WINDOW_MINUTES)
        row = db.execute(
            select(
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.content_guard_result == "block", 1), else_=0)).label("block_count"),
                func.sum(case((RequestLog.content_guard_result == "review", 1), else_=0)).label("review_count"),
                func.sum(case((RequestLog.content_guard_risk_level == "high", 1), else_=0)).label("high_risk_count"),
            ).where(
                RequestLog.created_at >= since,
                RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
            )
        ).one()
        total_requests = int(row.total_requests or 0)
        guard_latencies = list(
            db.scalars(
                select(RequestLog.content_guard_latency_ms)
                .where(
                    RequestLog.created_at >= since,
                    RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
                    RequestLog.content_guard_latency_ms.is_not(None),
                )
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(cls.METRIC_PERCENTILE_SAMPLE_LIMIT)
            )
        )
        low_trust_traffic = int(
            db.scalar(
                select(func.count(RequestLog.id))
                .select_from(RequestLog)
                .join(Provider, RequestLog.provider_id == Provider.id)
                .where(
                    RequestLog.created_at >= since,
                    RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
                    Provider.trust_level == "low",
                )
            )
            or 0
        )
        blocked_provider_count = int(
            db.scalar(
                select(func.count(Provider.id)).where(
                    or_(
                        Provider.trust_level == "blocked",
                        Provider.content_integrity_status == "blocked",
                    )
                )
            )
            or 0
        )
        provider_rows = db.execute(
            select(
                RequestLog.provider_id,
                RequestLog.provider_name,
                func.count(RequestLog.id).label("total_requests"),
                func.sum(
                    case(
                        (
                            or_(
                                RequestLog.content_guard_result == "block",
                                RequestLog.content_guard_risk_level == "high",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("high_risk_count"),
                func.sum(case((RequestLog.content_guard_result == "review", 1), else_=0)).label("review_count"),
            )
            .where(
                RequestLog.created_at >= since,
                RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
                RequestLog.provider_id.is_not(None),
            )
            .group_by(RequestLog.provider_id, RequestLog.provider_name)
        )
        provider_content_violation_rate = []
        for item in provider_rows:
            total = int(item.total_requests or 0)
            high_risk = int(item.high_risk_count or 0)
            review = int(item.review_count or 0)
            if high_risk <= 0 and review <= 0:
                continue
            provider_content_violation_rate.append(
                {
                    "provider_id": item.provider_id,
                    "provider_name": item.provider_name,
                    "total_requests": total,
                    "high_risk_count": high_risk,
                    "review_count": review,
                    "violation_rate": round(((high_risk + review) / total) * 100, 2) if total else 0.0,
                }
            )
        high_risk_rows = db.execute(
            select(
                RequestLog.provider_id,
                RequestLog.provider_name,
                func.count(RequestLog.id).label("high_risk_count"),
            )
            .where(
                RequestLog.created_at >= high_risk_since,
                RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
                RequestLog.provider_id.is_not(None),
                or_(
                    RequestLog.content_guard_result == "block",
                    RequestLog.content_guard_risk_level == "high",
                ),
            )
            .group_by(RequestLog.provider_id, RequestLog.provider_name)
        )
        high_risk_provider_counts = [
            {
                "provider_id": item.provider_id,
                "provider_name": item.provider_name,
                "high_risk_count": int(item.high_risk_count or 0),
                "window_minutes": cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_WINDOW_MINUTES,
            }
            for item in high_risk_rows
        ]
        return {
            "enabled": True,
            "block_count": int(row.block_count or 0),
            "review_count": int(row.review_count or 0),
            "high_risk_count": int(row.high_risk_count or 0),
            "blocked_provider_count": blocked_provider_count,
            "low_trust_traffic_count": low_trust_traffic,
            "low_trust_traffic_ratio": round((low_trust_traffic / total_requests) * 100, 2) if total_requests else 0.0,
            "latency_p50_ms": cls._percentile(guard_latencies, 50),
            "latency_p95_ms": cls._percentile(guard_latencies, 95),
            "latency_p99_ms": cls._percentile(guard_latencies, 99),
            "provider_content_violation_rate": provider_content_violation_rate,
            "high_risk_provider_counts": high_risk_provider_counts,
        }

    @classmethod
    def _provider_snapshot(cls, db: Session, *, window_minutes: int) -> list[dict[str, Any]]:
        since = datetime.utcnow() - timedelta(minutes=window_minutes)
        providers = list(db.scalars(select(Provider).order_by(Provider.id.asc())))
        provider_ids = {item.id for item in providers}
        try:
            capacity = ProviderCapacityService.snapshots(provider_ids) if provider_ids else {}
        except ProviderCapacityUnavailableError:
            capacity = {}
        rows = db.execute(
            select(
                RequestLog.provider_id,
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failed_requests"),
                func.avg(RequestLog.first_token_latency_ms).label("avg_first_token_latency_ms"),
            )
            .where(
                RequestLog.created_at >= since,
                RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
                RequestLog.provider_id.is_not(None),
            )
            .group_by(RequestLog.provider_id)
        )
        traffic_by_provider = {
            row.provider_id: {
                "total_requests": int(row.total_requests or 0),
                "failed_requests": int(row.failed_requests or 0),
                "avg_first_token_latency_ms": round(float(row.avg_first_token_latency_ms), 2)
                if row.avg_first_token_latency_ms is not None
                else None,
            }
            for row in rows
        }
        items = []
        for provider in providers:
            traffic = traffic_by_provider.get(provider.id, {"total_requests": 0, "failed_requests": 0, "avg_first_token_latency_ms": None})
            total_requests = traffic["total_requests"]
            failed_requests = traffic["failed_requests"]
            snapshot = capacity.get(provider.id)
            items.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "enabled": provider.enabled,
                    "health_status": provider.health_status,
                    "total_requests": total_requests,
                    "failed_requests": failed_requests,
                    "success_rate": round(((total_requests - failed_requests) / total_requests) * 100, 2) if total_requests else 100.0,
                    "failure_rate": round((failed_requests / total_requests) * 100, 2) if total_requests else 0.0,
                    "avg_first_token_latency_ms": traffic["avg_first_token_latency_ms"],
                    "active_requests": snapshot.active_requests if snapshot else 0,
                    "active_streams": snapshot.active_streams if snapshot else 0,
                    "current_qps": snapshot.current_qps if snapshot else 0,
                    "current_rpm": snapshot.current_rpm if snapshot else 0,
                    "max_active_requests": provider.max_active_requests,
                    "max_active_streams": provider.max_active_streams,
                    "max_qps": provider.max_qps,
                    "max_rpm": provider.max_rpm,
                }
            )
        return items

    @classmethod
    def _background_snapshot(cls, db: Session) -> dict[str, Any]:
        scheduler_jobs = len(scheduler.get_jobs()) if scheduler.running else 0
        pending_finalize = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._token_billing_finalize_candidate_expr(),
                    RequestLog.billing_finalized_at.is_(None),
                )
            ) or 0
        )
        billing_failed = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._token_billing_finalize_candidate_expr(),
                    RequestLog.billing_error.is_not(None),
                    RequestLog.billing_finalized_at.is_(None),
                )
            ) or 0
        )
        token_failed = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._token_billing_finalize_candidate_expr(),
                    RequestLog.token_finalize_error.is_not(None),
                    RequestLog.billing_finalized_at.is_(None),
                )
            ) or 0
        )
        return {
            "scheduler_running": scheduler.running,
            "scheduler_jobs": scheduler_jobs,
            "pending_finalize_logs": pending_finalize,
            "billing_failed_logs": billing_failed,
            "token_failed_logs": token_failed,
            "recent_failed_jobs": cls._recent_failed_background_jobs(db),
        }

    @staticmethod
    def _recent_failed_background_jobs(db: Session) -> list[dict[str, Any]]:
        rows = db.scalars(
            select(BackgroundJobEvent)
            .where(BackgroundJobEvent.status.in_(("failed", "cancelled")))
            .order_by(BackgroundJobEvent.created_at.desc())
            .limit(10)
        ).all()
        return [
            {
                "job_run_id": item.job_run_id,
                "job_name": item.job_name,
                "status": item.status,
                "lock_status": item.lock_status,
                "duration_ms": item.duration_ms,
                "error": item.error,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in rows
        ]

    @staticmethod
    def _empty_traffic() -> dict[str, Any]:
        return {
            "total_requests": 0,
            "success_requests": 0,
            "failed_requests": 0,
            "status_429": 0,
            "status_5xx": 0,
            "failure_rate": 0.0,
            "status_429_rate": 0.0,
            "status_5xx_rate": 0.0,
            "qps": 0.0,
            "stream_qps": 0.0,
            "p50_latency_ms": None,
            "p95_latency_ms": None,
            "p99_latency_ms": None,
            "avg_first_token_latency_ms": None,
            "stream_requests": 0,
            "image_requests": 0,
        }

    @staticmethod
    def _empty_background() -> dict[str, Any]:
        return {
            "scheduler_running": scheduler.running,
            "scheduler_jobs": len(scheduler.get_jobs()) if scheduler.running else 0,
            "pending_finalize_logs": None,
            "billing_failed_logs": None,
            "token_failed_logs": None,
            "recent_failed_jobs": [],
        }

    @staticmethod
    def _empty_content_guard(*, enabled: bool = True) -> dict[str, Any]:
        return {
            "enabled": enabled,
            "block_count": 0,
            "review_count": 0,
            "high_risk_count": 0,
            "blocked_provider_count": 0,
            "low_trust_traffic_count": 0,
            "low_trust_traffic_ratio": 0.0,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "latency_p99_ms": None,
            "provider_content_violation_rate": [],
            "high_risk_provider_counts": [],
        }

    @classmethod
    def _resolve_status(
        cls,
        *,
        database_ok: bool,
        redis_ok: bool,
        background: dict[str, Any],
        traffic: dict[str, Any],
    ) -> str:
        if not database_ok or not redis_ok:
            return "degraded"
        if (background.get("pending_finalize_logs") or 0) >= cls.BACKGROUND_BACKLOG_WARNING_THRESHOLD:
            return "degraded"
        if traffic.get("status_5xx_rate", 0.0) >= cls.ERROR_RATE_5XX_WARNING_THRESHOLD and traffic.get("total_requests", 0) >= 10:
            return "degraded"
        return "ready"

    @classmethod
    def _evaluate_alerts(cls, metrics: dict[str, Any]) -> list[dict[str, Any]]:
        return list(cls._build_monitoring_alert_events(metrics).values())

    @classmethod
    def _build_monitoring_alert_events(cls, metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
        events: dict[str, dict[str, Any]] = {}
        redis_snapshot = metrics.get("redis", {})
        database = metrics.get("database", {})
        traffic = metrics.get("traffic", {})
        content_guard = metrics.get("content_guard", {})
        background = metrics.get("background", {})
        active_requests = redis_snapshot.get("active_requests")
        active_streams = redis_snapshot.get("active_streams")
        active_request_threshold = cls._capacity_warning_threshold(
            redis_snapshot.get("max_active_requests"),
            cls.ACTIVE_REQUEST_WARNING_THRESHOLD,
        )
        active_stream_threshold = cls._capacity_warning_threshold(
            redis_snapshot.get("max_active_streams"),
            cls.ACTIVE_STREAM_WARNING_THRESHOLD,
        )
        if active_requests is not None and active_requests >= active_request_threshold:
            events["monitoring:global_active_requests"] = cls._event(
                "monitoring:global_active_requests",
                "failure_rate",
                "danger",
                "全局活跃请求接近容量上限",
                f"Redis 全局活跃请求 {active_requests}，阈值 {active_request_threshold}",
                {"active_requests": active_requests, "threshold": active_request_threshold},
            )
        if active_streams is not None and active_streams >= active_stream_threshold:
            events["monitoring:global_active_streams"] = cls._event(
                "monitoring:global_active_streams",
                "failure_rate",
                "danger",
                "全局流式请求达到容量上限",
                f"Redis 全局活跃流式请求 {active_streams}，阈值 {active_stream_threshold}",
                {"active_streams": active_streams, "threshold": active_stream_threshold},
            )
        if not redis_snapshot.get("ok"):
            events["monitoring:redis_unavailable"] = cls._event(
                "monitoring:redis_unavailable",
                "failure_rate",
                "danger",
                "Redis 不可用",
                str(redis_snapshot.get("error") or "Redis ping failed"),
                redis_snapshot,
            )
        if not database.get("ok"):
            events["monitoring:database_unavailable"] = cls._event(
                "monitoring:database_unavailable",
                "failure_rate",
                "danger",
                "数据库不可用",
                str(database.get("error") or "database ping failed"),
                database,
            )
        if traffic.get("total_requests", 0) >= 10 and traffic.get("status_5xx_rate", 0.0) >= cls.ERROR_RATE_5XX_WARNING_THRESHOLD:
            events["monitoring:status_5xx_rate"] = cls._event(
                "monitoring:status_5xx_rate",
                "failure_rate",
                "danger",
                "5xx 错误率过高",
                f"最近 {metrics.get('window_minutes')} 分钟 5xx 错误率 {traffic['status_5xx_rate']}%",
                traffic,
            )
        if (
            traffic.get("status_429", 0) >= cls.ERROR_COUNT_429_WARNING_THRESHOLD
            or traffic.get("status_429_rate", 0.0) >= cls.ERROR_RATE_429_WARNING_THRESHOLD
        ):
            events["monitoring:status_429_spike"] = cls._event(
                "monitoring:status_429_spike",
                "failure_rate",
                "warning",
                "429 限流响应突增",
                f"最近 {metrics.get('window_minutes')} 分钟 429 数量 {traffic.get('status_429', 0)}",
                traffic,
            )
        for provider in metrics.get("providers", []):
            if provider.get("total_requests", 0) < 10:
                continue
            if provider.get("failure_rate", 0.0) < cls.PROVIDER_FAILURE_RATE_WARNING_THRESHOLD:
                continue
            key = f"monitoring:provider_failure_rate:{provider['provider_id']}"
            events[key] = cls._event(
                key,
                "provider",
                "danger",
                f"Provider 失败率过高 · {provider['provider_name']}",
                f"最近 {metrics.get('window_minutes')} 分钟失败率 {provider['failure_rate']}%",
                provider,
            )
        if content_guard.get("enabled", True):
            for provider in content_guard.get("high_risk_provider_counts", []):
                if int(provider.get("high_risk_count") or 0) < cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_THRESHOLD:
                    continue
                provider_id = provider.get("provider_id")
                key = f"monitoring:content_guard_high_risk_provider:{provider_id}"
                events[key] = cls._event(
                    key,
                    "provider",
                    "danger",
                    f"提供商内容高风险命中突增 · {provider.get('provider_name') or provider_id}",
                    f"最近 {provider.get('window_minutes', cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_WINDOW_MINUTES)} 分钟内容高风险命中 {provider.get('high_risk_count')} 次，将执行自动隔离并从路由候选排除",
                    provider,
                )
        pending_finalize = background.get("pending_finalize_logs")
        if pending_finalize is not None and pending_finalize >= cls.BACKGROUND_BACKLOG_WARNING_THRESHOLD:
            events["monitoring:background_backlog"] = cls._event(
                "monitoring:background_backlog",
                "failure_rate",
                "warning",
                "后台任务积压过高",
                f"待补全日志 {pending_finalize}，阈值 {cls.BACKGROUND_BACKLOG_WARNING_THRESHOLD}",
                background,
            )
        billing_failed = background.get("billing_failed_logs")
        if billing_failed is not None and billing_failed >= cls.BILLING_FAILURE_WARNING_THRESHOLD:
            events["monitoring:billing_failed"] = cls._event(
                "monitoring:billing_failed",
                "failure_rate",
                "danger",
                "计费失败数量过高",
                f"未完成计费且存在错误的日志 {billing_failed} 条",
                background,
            )
        token_failed = background.get("token_failed_logs")
        if token_failed is not None and token_failed >= cls.TOKEN_FAILURE_WARNING_THRESHOLD:
            events["monitoring:token_finalize_failed"] = cls._event(
                "monitoring:token_finalize_failed",
                "failure_rate",
                "warning",
                "Token 回填失败数量过高",
                f"Token 回填失败且未完成计费的日志 {token_failed} 条",
                background,
            )
        return events

    @staticmethod
    def _capacity_warning_threshold(configured_limit: Any, fallback: int) -> int:
        limit = int(configured_limit or 0)
        if limit <= 0:
            return fallback
        return max(1, int(limit * 0.9))

    @staticmethod
    def _event(
        alert_key: str,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "alert_key": alert_key,
            "alert_type": alert_type,
            "severity": severity,
            "title": title,
            "message": message,
            "payload": payload,
        }

    @staticmethod
    def _percentile(values: list[Any], percentile: int) -> float | None:
        if not values:
            return None
        ordered = sorted(float(item) for item in values if item is not None)
        if not ordered:
            return None
        if len(ordered) == 1:
            return round(ordered[0], 2)
        rank = max(0.0, min(1.0, percentile / 100)) * (len(ordered) - 1)
        lower = int(rank)
        upper = min(len(ordered) - 1, lower + 1)
        fraction = rank - lower
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 2)
