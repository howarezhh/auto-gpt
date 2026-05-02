from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from redis import Redis
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import engine
from app.models.alert_event import AlertEvent
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.scheduler import scheduler
from app.services.concurrency_service import ConcurrencyService
from app.services.log_service import LogService
from app.services.provider_capacity_service import ProviderCapacityService, ProviderCapacityUnavailableError
from app.services.runtime_state_service import RuntimeStateService
from app.utils.json_utils import dumps_json


class SystemMetricsService:
    TRAFFIC_LOG_TYPES = ("chat", "responses")
    ACTIVE_REQUEST_WARNING_THRESHOLD = 900
    ACTIVE_STREAM_WARNING_THRESHOLD = 300
    ERROR_RATE_5XX_WARNING_THRESHOLD = 1.0
    ERROR_RATE_429_WARNING_THRESHOLD = 5.0
    ERROR_COUNT_429_WARNING_THRESHOLD = 5
    PROVIDER_FAILURE_RATE_WARNING_THRESHOLD = 20.0
    BACKGROUND_BACKLOG_WARNING_THRESHOLD = 1000
    BILLING_FAILURE_WARNING_THRESHOLD = 10
    TOKEN_FAILURE_WARNING_THRESHOLD = 10
    METRIC_PERCENTILE_SAMPLE_LIMIT = 5000

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
        redis_snapshot = cls._redis_snapshot()
        runtime = cls._runtime_snapshot()
        traffic = cls._traffic_snapshot(db, window_minutes=window_minutes) if database["ok"] else cls._empty_traffic()
        providers = cls._provider_snapshot(db, window_minutes=window_minutes) if database["ok"] else []
        background = cls._background_snapshot(db) if database["ok"] else cls._empty_background()
        pool = cls._database_pool_snapshot()
        status = cls._resolve_status(
            database_ok=database["ok"],
            redis_ok=redis_snapshot["ok"],
            background=background,
            traffic=traffic,
        )
        metrics = {
            "status": status,
            "window_minutes": window_minutes,
            "generated_at": datetime.utcnow().isoformat(),
            "database": {**database, "pool": pool},
            "redis": redis_snapshot,
            "runtime": runtime,
            "traffic": traffic,
            "providers": providers,
            "background": background,
        }
        metrics["alerts"] = cls._evaluate_alerts(metrics)
        if refresh_alerts and database["ok"]:
            cls.write_monitoring_alerts(db, metrics)
        return metrics

    @classmethod
    def write_monitoring_alerts(cls, db: Session, metrics: dict[str, Any]) -> None:
        active_events = cls._build_monitoring_alert_events(metrics)
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
        changed = False
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
                return {
                    "ok": True,
                    "status": "ok",
                    "latency_ms": latency_ms,
                    "active_requests": int(active_values[0] or 0),
                    "active_streams": int(active_values[1] or 0),
                    "token_finalize_backlog": cls._count_redis_keys(client, "token_usage:finalize:dedupe:*", limit=1001),
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
                "error": str(exc),
            }

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
                    "max_active_requests": provider.max_active_requests,
                    "max_active_streams": provider.max_active_streams,
                    "max_qps": provider.max_qps,
                }
            )
        return items

    @classmethod
    def _background_snapshot(cls, db: Session) -> dict[str, Any]:
        scheduler_jobs = len(scheduler.get_jobs()) if scheduler.running else 0
        pending_finalize = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    RequestLog.request_path.is_not(None),
                    RequestLog.request_path != "/v1/models",
                    LogService._non_health_check_expr(),
                    RequestLog.billing_finalized_at.is_(None),
                )
            ) or 0
        )
        billing_failed = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._non_health_check_expr(),
                    RequestLog.billing_error.is_not(None),
                    RequestLog.billing_finalized_at.is_(None),
                )
            ) or 0
        )
        token_failed = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._non_health_check_expr(),
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
        }

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
        background = metrics.get("background", {})
        active_requests = redis_snapshot.get("active_requests")
        active_streams = redis_snapshot.get("active_streams")
        if active_requests is not None and active_requests >= cls.ACTIVE_REQUEST_WARNING_THRESHOLD:
            events["monitoring:global_active_requests"] = cls._event(
                "monitoring:global_active_requests",
                "failure_rate",
                "danger",
                "全局活跃请求接近容量上限",
                f"Redis 全局活跃请求 {active_requests}，阈值 {cls.ACTIVE_REQUEST_WARNING_THRESHOLD}",
                {"active_requests": active_requests, "threshold": cls.ACTIVE_REQUEST_WARNING_THRESHOLD},
            )
        if active_streams is not None and active_streams >= cls.ACTIVE_STREAM_WARNING_THRESHOLD:
            events["monitoring:global_active_streams"] = cls._event(
                "monitoring:global_active_streams",
                "failure_rate",
                "danger",
                "全局流式请求达到容量上限",
                f"Redis 全局活跃流式请求 {active_streams}，阈值 {cls.ACTIVE_STREAM_WARNING_THRESHOLD}",
                {"active_streams": active_streams, "threshold": cls.ACTIVE_STREAM_WARNING_THRESHOLD},
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
