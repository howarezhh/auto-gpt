from __future__ import annotations

import os
import platform
import time
from collections import Counter
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

try:
    import psutil
except ImportError:  # deployment fallback
    psutil = None

from redis import Redis
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import engine
from app.models.alert_event import AlertEvent
from app.models.logging_events import BackgroundJobEvent, RequestContentGuardEvent
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.scheduler import scheduler
from app.services.log_service import LogService
from app.logging.queue import LoggingQueue
from app.services.cache_service import CacheService
from app.services.redis_service import RedisService
from app.services.request_log_queue_service import RequestLogQueueService
from app.services.provider_capacity_service import ProviderCapacityService, ProviderCapacityUnavailableError
from app.services.provider_service import ProviderService
from app.services.runtime_state_service import RuntimeStateService
from app.services.setting_service import SettingService
from app.services.token_usage_service import TokenUsageService
from app.utils.json_utils import dumps_json, loads_json


PROCESS_STARTED_AT = time.time()


class SystemMetricsService:
    TRAFFIC_LOG_TYPES = ("chat", "responses", "moderations", "files")
    ACTIVE_REQUEST_WARNING_THRESHOLD = 900
    ACTIVE_STREAM_WARNING_THRESHOLD = 300
    MONITORING_ALERT_LAST_SEEN_REFRESH_SECONDS = 60
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
    CONTENT_GUARD_ISOLATION_SNAPSHOT_MODEL_LIMIT = 50
    METRIC_PERCENTILE_SAMPLE_LIMIT = 5000
    PROVIDER_METRIC_LIMIT = 200
    PROCESS_SCAN_LIMIT = 500
    DISK_USAGE_WARNING_PERCENT = 90.0
    MEMORY_USAGE_WARNING_PERCENT = 90.0
    FD_USAGE_WARNING_RATIO = 0.85
    CGROUP_USAGE_WARNING_PERCENT = 90.0
    TCP_TIME_WAIT_WARNING_THRESHOLD = 5000
    IO_WAIT_WARNING_PERCENT = 20.0
    LOAD_PER_CPU_WARNING_THRESHOLD = 1.5
    EVENT_LOOP_DELAY_WARNING_MS = 200.0
    EVENT_LOOP_DELAY_DANGER_MS = 1000.0
    QUEUE_WAIT_WARNING_MS = 2000.0
    CLIENT_CANCEL_WARNING_THRESHOLD = 10
    CONFIGURATION_PROFILE_SETTING_FIELDS = (
        "global_qps_limit",
        "global_rpm_limit",
        "account_qps_limit",
        "account_rpm_limit",
        "global_max_active_requests",
        "global_max_active_streams",
        "api_key_max_active_requests",
        "api_key_max_active_streams",
        "account_max_active_requests",
        "account_max_active_streams",
        "provider_max_active_requests",
        "provider_max_active_streams",
        "concurrency_lease_ttl_seconds",
        "stream_connect_timeout_seconds",
        "stream_first_token_timeout_seconds",
        "stream_idle_timeout_seconds",
        "stream_max_duration_seconds",
        "max_v1_request_body_bytes",
        "max_v1_chat_request_body_bytes",
        "max_v1_responses_request_body_bytes",
        "long_output_stream_threshold_tokens",
        "max_non_stream_response_body_bytes",
        "stream_token_capture_max_bytes",
        "max_logged_metadata_bytes",
        "max_logged_body_bytes",
        "async_request_logging",
        "max_candidate_count",
        "route_candidate_expand_count",
        "route_candidate_cache_ttl_sec",
        "model_list_cache_ttl_sec",
        "provider_status_cache_ttl_sec",
    )
    MONITORING_ALERT_RESOLVE_BATCH_SIZE = 500
    MONITORING_ALERT_RESOLVE_MAX_BATCHES = 100
    PROJECT_PROCESS_CACHE_SECONDS = 30
    BACKGROUND_SNAPSHOT_CACHE_SECONDS = 5
    DB_SECTION_CACHE_SECONDS = 3
    _project_process_cache: dict[str, Any] = {
        "path": None,
        "snapshot": None,
        "checked_at": 0.0,
        "cpu_times": {},
        "cpu_checked_at": 0.0,
    }
    _background_snapshot_cache: dict[str, Any] = {"snapshot": None, "checked_at": 0.0}
    _network_io_cache: dict[str, Any] = {
        "checked_at": 0.0,
        "bytes_sent": None,
        "bytes_recv": None,
    }

    @classmethod
    def collect(
        cls,
        db: Session,
        *,
        window_minutes: int = 5,
        refresh_alerts: bool = False,
        network_bandwidth_mbps: float | None = None,
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
            cls._safe_cached_snapshot(
                db,
                section_errors,
                "traffic",
                f"system-metrics:traffic:{window_minutes}",
                lambda: cls._traffic_snapshot(db, window_minutes=window_minutes),
                cls._empty_traffic,
            )
            if database["ok"]
            else cls._empty_traffic()
        )
        bucket_minutes = cls._bucket_minutes(window_minutes)
        timeseries = (
            cls._safe_cached_snapshot(
                db,
                section_errors,
                "timeseries",
                f"system-metrics:timeseries:{window_minutes}:{bucket_minutes}",
                lambda: LogService.metric_timeseries(db, window_minutes=window_minutes, bucket_minutes=bucket_minutes),
                list,
            )
            if database["ok"]
            else []
        )
        providers = (
            cls._safe_cached_snapshot(
                db,
                section_errors,
                "providers",
                f"system-metrics:providers:{window_minutes}",
                lambda: cls._provider_snapshot(db, window_minutes=window_minutes),
                list,
            )
            if database["ok"]
            else []
        )
        content_guard = (
            cls._safe_cached_snapshot(
                db,
                section_errors,
                "content_guard",
                f"system-metrics:content-guard:{window_minutes}",
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
        redis_snapshot["token_finalize_backlog"] = background.get("pending_finalize_logs")
        redis_snapshot["token_finalize_backlog_source"] = "request_logs_pending_finalize"
        pool = cls._database_pool_snapshot()
        status = cls._resolve_status(
            database_ok=database["ok"],
            redis_ok=redis_snapshot["ok"],
            background=background,
            traffic=traffic,
        )
        if section_errors and status == "ready":
            status = "degraded"
        setting_snapshot = None
        if database["ok"]:
            try:
                setting_snapshot = SettingService.get_or_create(db)
            except Exception as exc:
                rollback = getattr(db, "rollback", None)
                if callable(rollback):
                    rollback()
                section_errors["configuration_recommendations"] = str(exc)
        configuration_recommendations = cls._configuration_recommendations(
            host=host,
            database=database,
            redis_snapshot=redis_snapshot,
            background=background,
            traffic=traffic,
            setting=setting_snapshot,
            network_bandwidth_mbps=network_bandwidth_mbps,
        )
        configuration_effectiveness = cls._configuration_effectiveness_snapshot()
        if section_errors and status == "ready":
            status = "degraded"
        metrics = {
            "status": status,
            "window_minutes": window_minutes,
            "generated_at": now_beijing().isoformat(),
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
            "configuration_recommendations": configuration_recommendations,
            "configuration_effectiveness": configuration_effectiveness,
            "section_errors": section_errors,
        }
        metrics["alerts"] = cls._evaluate_alerts(metrics)
        if database["ok"]:
            cls.apply_monitoring_actions(db, metrics)
            if refresh_alerts:
                cls.write_monitoring_alerts(db, metrics)
        return metrics

    @classmethod
    def _configuration_recommendations(
        cls,
        *,
        host: dict[str, Any],
        database: dict[str, Any],
        redis_snapshot: dict[str, Any],
        background: dict[str, Any],
        traffic: dict[str, Any],
        setting: Any | None,
        network_bandwidth_mbps: float | None,
    ) -> dict[str, Any]:
        cpu_count = int(host.get("cpu_count") or os.cpu_count() or 0)
        memory_total_bytes = int((host.get("memory") or {}).get("total_bytes") or 0)
        memory_available_bytes = int((host.get("memory") or {}).get("available_bytes") or 0)
        project_process = host.get("project_process") or {}
        current_process = host.get("process") or {}
        project_memory_rss_bytes = int(
            project_process.get("memory_rss_bytes")
            or current_process.get("memory_rss_bytes")
            or 0
        )
        runtime_settings = get_settings()
        current_settings: dict[str, Any] = {}
        if setting is not None:
            for field in cls.CONFIGURATION_PROFILE_SETTING_FIELDS:
                value = getattr(setting, field, None)
                if isinstance(value, bool):
                    current_settings[field] = value
                elif value is None:
                    current_settings[field] = None
                else:
                    current_settings[field] = int(value or 0)
        bandwidth_mbps, bandwidth_source = cls._manual_network_bandwidth_mbps(network_bandwidth_mbps)
        detected = {
            "cpu_count": cpu_count,
            "memory_total_bytes": memory_total_bytes,
            "memory_available_bytes": memory_available_bytes or None,
            "project_memory_rss_bytes": project_memory_rss_bytes or None,
            "memory_gb": round(memory_total_bytes / (1024**3), 2) if memory_total_bytes else None,
            "platform": host.get("platform"),
            "host_metrics_provider": host.get("provider"),
            "database_ok": bool(database.get("ok")),
            "redis_ok": bool(redis_snapshot.get("ok")),
            "request_log_queue_total": (redis_snapshot.get("request_log_queue") or {}).get("total"),
            "logging_event_queue_total": (redis_snapshot.get("logging_event_queue") or {}).get("total"),
            "pending_finalize_logs": background.get("pending_finalize_logs"),
            "network_bandwidth_mbps": bandwidth_mbps or None,
            "network_bandwidth_source": bandwidth_source,
            "network_bandwidth_required": bandwidth_mbps <= 0,
        }
        network_pressure = cls._network_pressure_snapshot(host.get("network"), bandwidth_mbps)
        if network_pressure:
            detected["network_pressure"] = network_pressure
        if cpu_count <= 0 or memory_total_bytes <= 0:
            return {
                "available": False,
                "reason": "缺少 CPU 核数或总内存，无法给出可信宿主配置推荐。",
                "host_class": "未知",
                "detected": detected,
                "current_settings": current_settings,
                "recommended_profile_id": None,
                "profiles": [],
            }
        if bandwidth_mbps <= 0:
            return {
                "available": False,
                "reason": "公网出口带宽不做自动获取，请手动输入宿主带宽 Mbps 后生成配置方案。",
                "host_class": cls._host_resource_class(cpu_count, max(0.5, memory_total_bytes / (1024**3))),
                "detected": detected,
                "current_settings": current_settings,
                "recommended_profile_id": None,
                "profiles": [],
                "requires_network_bandwidth": True,
                "calculation_method": "公网带宽属于云厂商规格或机房链路上限，宿主流量计数只能反映历史收发字节，不能反推出真实上限；本项目仅使用管理员手动输入值做容量推荐。",
            }

        memory_gb = max(0.5, memory_total_bytes / (1024**3))
        runtime_snapshot = {
            "web_concurrency": int(runtime_settings.web_concurrency or 1),
            "db_pool_size": int(runtime_settings.db_pool_size or 0),
            "db_max_overflow": int(runtime_settings.db_max_overflow or 0),
            "db_total_connections": int(runtime_settings.db_pool_size or 0) + int(runtime_settings.db_max_overflow or 0),
            "upstream_max_connections": int(runtime_settings.upstream_max_connections or 0),
            "upstream_max_keepalive_connections": int(runtime_settings.upstream_max_keepalive_connections or 0),
            "worker_threadpool_tokens": int(runtime_settings.worker_threadpool_tokens or 0),
            "redis_max_connections": int(runtime_settings.redis_max_connections or 0),
            "request_log_queue_worker_count": int(runtime_settings.request_log_queue_worker_count or 0),
            "logging_event_queue_worker_count": int(runtime_settings.logging_event_queue_worker_count or 0),
            "token_finalize_worker_count": int(runtime_settings.token_finalize_worker_count or 0),
        }
        stream_ratio = 0.0
        total_requests = int(traffic.get("total_requests") or 0)
        if total_requests > 0:
            stream_ratio = min(1.0, max(0.0, float(traffic.get("stream_requests") or 0) / total_requests))
        host_class = cls._host_resource_class(cpu_count, memory_gb)
        profile_specs = [
            {
                "id": "conservative",
                "name": "保守稳定",
                "summary": "低资源占用，适合 2C2G 或后台任务与 Web 混跑的轻量云。",
                "active_cpu": 16,
                "active_cap": 200,
                "stream_cpu": 6,
                "stream_cap": 80,
                "target_stream_ratio": 0.25,
                "memory_reserve_ratio": 0.42,
                "non_stream_memory_mb": 12,
                "stream_memory_mb": 30,
                "db_request_multiplier": 8,
                "candidate_count": 8,
                "cache_ttls": (10, 30, 10),
                "ttl": 900,
                "workers_cap": 2,
                "db_pool_per_worker": 6,
                "queue_workers": 1,
                "bandwidth_utilization_ratio": 0.42,
                "non_stream_network_kbps": 96,
                "stream_network_kbps": 48,
                "global_qps_divisor": 16,
                "request_body_bytes": 5 * 1024 * 1024,
                "non_stream_response_bytes": 8 * 1024 * 1024,
                "stream_capture_bytes": 256 * 1024,
                "logged_body_bytes": 8 * 1024,
                "logged_metadata_bytes": 1024,
            },
            {
                "id": "balanced",
                "name": "均衡生产",
                "summary": "兼顾吞吐和余量，适合 4C8G 到 8C16G 的常规单机生产。",
                "active_cpu": 32,
                "active_cap": 600,
                "stream_cpu": 12,
                "stream_cap": 180,
                "target_stream_ratio": 0.3,
                "memory_reserve_ratio": 0.35,
                "non_stream_memory_mb": 10,
                "stream_memory_mb": 26,
                "db_request_multiplier": 10,
                "candidate_count": 12,
                "cache_ttls": (10, 60, 15),
                "ttl": 900,
                "workers_cap": 6,
                "db_pool_per_worker": 8,
                "queue_workers": 2,
                "bandwidth_utilization_ratio": 0.55,
                "non_stream_network_kbps": 128,
                "stream_network_kbps": 64,
                "global_qps_divisor": 12,
                "request_body_bytes": 10 * 1024 * 1024,
                "non_stream_response_bytes": 12 * 1024 * 1024,
                "stream_capture_bytes": 512 * 1024,
                "logged_body_bytes": 12 * 1024,
                "logged_metadata_bytes": 2048,
            },
            {
                "id": "throughput",
                "name": "高吞吐",
                "summary": "放大并发窗口，适合 Redis、数据库、上游连接池都独立扩容后的高流量节点。",
                "active_cpu": 56,
                "active_cap": 1200,
                "stream_cpu": 18,
                "stream_cap": 320,
                "target_stream_ratio": 0.28,
                "memory_reserve_ratio": 0.3,
                "non_stream_memory_mb": 9,
                "stream_memory_mb": 24,
                "db_request_multiplier": 12,
                "candidate_count": 20,
                "cache_ttls": (15, 120, 20),
                "ttl": 600,
                "workers_cap": 8,
                "db_pool_per_worker": 10,
                "queue_workers": 3,
                "bandwidth_utilization_ratio": 0.65,
                "non_stream_network_kbps": 160,
                "stream_network_kbps": 72,
                "global_qps_divisor": 10,
                "request_body_bytes": 16 * 1024 * 1024,
                "non_stream_response_bytes": 16 * 1024 * 1024,
                "stream_capture_bytes": 768 * 1024,
                "logged_body_bytes": 16 * 1024,
                "logged_metadata_bytes": 2048,
            },
            {
                "id": "streaming",
                "name": "流式优先",
                "summary": "给长连接保留更多窗口，适合流式请求占比较高、上游连接池已同步扩容的场景。",
                "active_cpu": 56,
                "active_cap": 800,
                "stream_cpu": 48,
                "stream_cap": 900,
                "stream_active_ratio": 1.0,
                "target_stream_ratio": 0.65,
                "memory_reserve_ratio": 0.34,
                "non_stream_memory_mb": 10,
                "stream_memory_mb": 6,
                "db_request_multiplier": 10,
                "candidate_count": 16,
                "cache_ttls": (10, 60, 15),
                "ttl": 900,
                "workers_cap": 6,
                "db_pool_per_worker": 8,
                "queue_workers": 2,
                "bandwidth_utilization_ratio": 0.6,
                "non_stream_network_kbps": 120,
                "stream_network_kbps": 72,
                "global_qps_divisor": 12,
                "request_body_bytes": 10 * 1024 * 1024,
                "non_stream_response_bytes": 12 * 1024 * 1024,
                "stream_capture_bytes": 384 * 1024,
                "logged_body_bytes": 12 * 1024,
                "logged_metadata_bytes": 2048,
            },
        ]
        recommended_profile_id = cls._recommended_profile_id(
            cpu_count=cpu_count,
            memory_gb=memory_gb,
            stream_ratio=stream_ratio,
            redis_ok=bool(redis_snapshot.get("ok")),
            background_backlog=int(background.get("pending_finalize_logs") or 0),
        )
        profiles = [
            cls._build_configuration_profile(
                spec,
                cpu_count=cpu_count,
                memory_gb=memory_gb,
                memory_total_bytes=memory_total_bytes,
                memory_available_bytes=memory_available_bytes,
                project_memory_rss_bytes=project_memory_rss_bytes,
                runtime_snapshot=runtime_snapshot,
                current_settings=current_settings,
                network_bandwidth_mbps=bandwidth_mbps,
                bandwidth_source=bandwidth_source,
                recommended=spec["id"] == recommended_profile_id,
            )
            for spec in profile_specs
        ]
        return {
            "available": True,
            "host_class": host_class,
            "detected": detected,
            "current_settings": current_settings,
            "runtime_snapshot": runtime_snapshot,
            "calculation_method": "按 CPU、可用内存、当前项目进程内存、建议数据库连接池、建议上游连接池和出口带宽分别估算容量，并取最小约束作为该方案峰值。",
            "recommended_profile_id": recommended_profile_id,
            "stream_ratio": round(stream_ratio, 4),
            "network_pressure_policy": {
                "runtime_enforcement": "concurrency_and_rate_limits",
                "byte_level_shaping": False,
                "summary": "项目会通过推荐的全局并发、流式并发、QPS/RPM 和请求/响应体上限提前限流或拒绝；当前没有按字节速率做内核级整形，带宽超压会表现为延迟升高、慢客户端和上游/下游连接占用增加。",
            },
            "profiles": profiles,
        }

    @staticmethod
    def _network_pressure_snapshot(network: Any, bandwidth_mbps: float) -> dict[str, Any] | None:
        if bandwidth_mbps <= 0 or not isinstance(network, dict):
            return None
        io = network.get("io") if isinstance(network.get("io"), dict) else {}
        observed_mbps = io.get("total_mbps")
        if not isinstance(observed_mbps, (int, float)):
            return {
                "available": False,
                "reason": "需要至少两次监控采样后才能计算当前网络速率。",
            }
        usage_ratio = float(observed_mbps) / max(0.01, float(bandwidth_mbps))
        if usage_ratio >= 0.9:
            level = "danger"
        elif usage_ratio >= 0.75:
            level = "warn"
        else:
            level = "ok"
        return {
            "available": True,
            "observed_mbps": round(float(observed_mbps), 2),
            "bandwidth_mbps": round(float(bandwidth_mbps), 2),
            "usage_ratio": round(usage_ratio, 4),
            "level": level,
        }

    @staticmethod
    def _configuration_effectiveness_snapshot() -> dict[str, Any]:
        settings = get_settings()
        return {
            "summary": "数据库系统设置更新后会通过缓存失效热生效；环境变量、Gunicorn worker 数、DB engine 连接池、Redis 连接池和上游连接池属于进程启动配置，修改后必须重启 Web/后台进程。",
            "sources": [
                {"name": "环境变量/.env", "scope": "进程启动配置", "effective": "restart_required"},
                {"name": "数据库 app_settings", "scope": "运行时系统设置", "effective": "hot_reload"},
                {"name": "提供商设置", "scope": "路由与上游调用", "effective": "hot_reload_after_cache_invalidation"},
                {"name": "API Key 设置", "scope": "鉴权、权限、限流与路由上下文", "effective": "hot_reload_after_cache_invalidation"},
            ],
            "hot_reload": {
                "runtime_settings_cache_ttl_seconds": SettingService.RUNTIME_CACHE_TTL_SECONDS,
                "route_cache_prefixes_invalidated_on_update": ["providers-runtime", "route-candidates", "v1-models"],
            },
            "restart_required": {
                "env_cached_by_settings": True,
                "fields": [
                    "DATABASE_URL",
                    "DB_POOL_SIZE",
                    "DB_MAX_OVERFLOW",
                    "DB_POOL_TIMEOUT",
                    "DB_POOL_RECYCLE",
                    "WEB_CONCURRENCY",
                    "GUNICORN_TIMEOUT",
                    "GUNICORN_KEEPALIVE",
                    "GUNICORN_GRACEFUL_TIMEOUT",
                    "WORKER_THREADPOOL_TOKENS",
                    "REDIS_URL",
                    "REDIS_MAX_CONNECTIONS",
                    "REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS",
                    "REDIS_SOCKET_TIMEOUT_SECONDS",
                    "ENABLE_BACKGROUND_WORKERS",
                    "ENABLE_SCHEDULER",
                    "REQUEST_LOG_QUEUE_WORKER_COUNT",
                    "REQUEST_LOG_QUEUE_BATCH_SIZE",
                    "REQUEST_LOG_INGRESS_QUEUE_SIZE",
                    "LOGGING_EVENT_QUEUE_WORKER_COUNT",
                    "LOGGING_EVENT_QUEUE_BATCH_SIZE",
                    "TOKEN_FINALIZE_WORKER_COUNT",
                    "TOKEN_FINALIZE_QUEUE_SIZE",
                    "REQUEST_TIMEOUT_MS",
                    "V1_REQUEST_BODY_IDLE_TIMEOUT_SECONDS",
                    "UPSTREAM_MAX_CONNECTIONS",
                    "UPSTREAM_MAX_KEEPALIVE_CONNECTIONS",
                    "UPSTREAM_POOL_TIMEOUT_S",
                    "UPSTREAM_KEEPALIVE_EXPIRY_SECONDS",
                    "UPSTREAM_DNS_CACHE_TTL_SECONDS",
                    "UPSTREAM_REQUESTS_POOL_BLOCK",
                    "ROUTE_CAPACITY_PREFILTER_ENABLED",
                ],
                "current_runtime": {
                    "web_concurrency": settings.web_concurrency,
                    "db_pool_size": settings.db_pool_size,
                    "db_max_overflow": settings.db_max_overflow,
                    "redis_url_configured": bool(settings.redis_url.strip()),
                    "enable_background_workers": settings.enable_background_workers,
                    "enable_scheduler": settings.enable_scheduler,
                },
            },
        }

    @staticmethod
    def _host_resource_class(cpu_count: int, memory_gb: float) -> str:
        if cpu_count < 2 or memory_gb < 2:
            return "开发/极低配"
        if cpu_count <= 2 or memory_gb < 6:
            return "轻量云"
        if cpu_count < 8 or memory_gb < 16:
            return "标准单机"
        return "高吞吐单机"

    @staticmethod
    def _recommended_profile_id(
        *,
        cpu_count: int,
        memory_gb: float,
        stream_ratio: float,
        redis_ok: bool,
        background_backlog: int,
    ) -> str:
        if cpu_count <= 2 or memory_gb < 4 or background_backlog >= 1000 or not redis_ok:
            return "conservative"
        if stream_ratio >= 0.35 and cpu_count >= 4 and memory_gb >= 8:
            return "streaming"
        if cpu_count >= 8 and memory_gb >= 16:
            return "throughput"
        return "balanced"

    @staticmethod
    def _manual_network_bandwidth_mbps(value: float | None) -> tuple[float, str]:
        if value is not None and float(value or 0) > 0:
            return min(100000.0, max(0.0, float(value))), "manual"
        return 0.0, "required"

    @classmethod
    def _build_configuration_profile(
        cls,
        spec: dict[str, Any],
        *,
        cpu_count: int,
        memory_gb: float,
        memory_total_bytes: int,
        memory_available_bytes: int,
        project_memory_rss_bytes: int,
        runtime_snapshot: dict[str, Any],
        current_settings: dict[str, Any],
        network_bandwidth_mbps: float,
        bandwidth_source: str,
        recommended: bool,
    ) -> dict[str, Any]:
        workers = max(1, min(cpu_count, spec["workers_cap"]))
        db_pool_size = max(5, min(80, workers * spec["db_pool_per_worker"]))
        db_max_overflow = max(5, min(40, round(db_pool_size * 0.5)))
        db_total_connections = db_pool_size + db_max_overflow
        db_multiplier = max(4, int(spec["db_request_multiplier"]))

        reserve_bytes = int(memory_total_bytes * float(spec["memory_reserve_ratio"]))
        target_app_budget_bytes = max(0, memory_total_bytes - reserve_bytes)
        measured_available_budget_bytes = int((memory_available_bytes or memory_total_bytes) * 0.8)
        app_budget_left_bytes = max(0, target_app_budget_bytes - project_memory_rss_bytes)
        usable_memory_bytes = min(measured_available_budget_bytes, app_budget_left_bytes)
        if usable_memory_bytes <= 0:
            usable_memory_bytes = int(memory_total_bytes * max(0.15, 1 - float(spec["memory_reserve_ratio"])) * 0.5)

        non_stream_memory_bytes = int(spec["non_stream_memory_mb"] * 1024 * 1024)
        stream_memory_bytes = int(spec["stream_memory_mb"] * 1024 * 1024)
        target_stream_ratio = min(0.8, max(0.05, float(spec["target_stream_ratio"])))
        mixed_request_memory_bytes = int(
            non_stream_memory_bytes * (1 - target_stream_ratio)
            + stream_memory_bytes * target_stream_ratio
        )
        cpu_active_limit = max(1, int(cpu_count * spec["active_cpu"]))
        memory_active_limit = max(1, int(usable_memory_bytes / max(1, mixed_request_memory_bytes)))
        db_active_limit = max(1, int(db_total_connections * db_multiplier))
        upstream_active_limit = max(10, min(5000, int(max(cpu_active_limit, memory_active_limit) * 1.35)))
        bandwidth_utilization_ratio = min(0.85, max(0.2, float(spec["bandwidth_utilization_ratio"])))
        usable_bandwidth_mbps = max(0.0, float(network_bandwidth_mbps) * bandwidth_utilization_ratio)
        non_stream_network_kbps = max(16.0, float(spec["non_stream_network_kbps"]))
        stream_network_kbps = max(16.0, float(spec["stream_network_kbps"]))
        bandwidth_active_limit = max(1, int((usable_bandwidth_mbps * 1000) / non_stream_network_kbps))
        active_limit_candidates = {
            "cpu": cpu_active_limit,
            "memory": memory_active_limit,
            "database": db_active_limit,
            "upstream": upstream_active_limit,
            "bandwidth": bandwidth_active_limit,
            "profile_cap": int(spec["active_cap"]),
        }
        active = cls._bounded_int(min(active_limit_candidates.values()), minimum=5, maximum=spec["active_cap"])

        cpu_stream_limit = max(1, int(cpu_count * spec["stream_cpu"]))
        memory_stream_limit = max(1, int((usable_memory_bytes * 0.62) / max(1, stream_memory_bytes)))
        # 流式请求不会长期持有数据库会话，DB 主要承担鉴权、路由和最终日志/计费写入。
        db_stream_limit = max(1, int(db_total_connections * max(8, db_multiplier * 1.2)))
        upstream_stream_limit = max(1, int(max(cpu_stream_limit, memory_stream_limit) * 1.1))
        bandwidth_stream_limit = max(1, int((usable_bandwidth_mbps * 1000) / stream_network_kbps))
        stream_limit_candidates = {
            "cpu": cpu_stream_limit,
            "memory": memory_stream_limit,
            "database": db_stream_limit,
            "upstream": upstream_stream_limit,
            "bandwidth": bandwidth_stream_limit,
            "active_ratio": max(1, int(active * float(spec.get("stream_active_ratio", 0.65)))),
            "profile_cap": int(spec["stream_cap"]),
        }
        streams = cls._bounded_int(min(stream_limit_candidates.values()), minimum=3, maximum=spec["stream_cap"])
        api_key_active = max(5, min(active, round(active * 0.35)))
        api_key_streams = max(3, min(streams, round(streams * 0.45)))
        account_active = max(api_key_active, min(active, round(active * 0.7)))
        account_streams = max(api_key_streams, min(streams, round(streams * 0.75)))
        provider_active = max(api_key_active, min(active, round(active * 0.5)))
        provider_streams = max(api_key_streams, min(streams, round(streams * 0.6)))
        route_ttl, model_ttl, provider_ttl = spec["cache_ttls"]
        global_qps_limit = max(1, int(active / max(1, int(spec["global_qps_divisor"]))))
        global_rpm_limit = max(global_qps_limit * 60, active * 2)
        account_qps_limit = max(1, int(global_qps_limit * 0.6))
        account_rpm_limit = max(account_qps_limit * 60, int(global_rpm_limit * 0.6))
        settings_patch = {
            "global_qps_limit": global_qps_limit,
            "global_rpm_limit": global_rpm_limit,
            "account_qps_limit": account_qps_limit,
            "account_rpm_limit": account_rpm_limit,
            "global_max_active_requests": active,
            "global_max_active_streams": streams,
            "api_key_max_active_requests": api_key_active,
            "api_key_max_active_streams": api_key_streams,
            "account_max_active_requests": account_active,
            "account_max_active_streams": account_streams,
            "provider_max_active_requests": provider_active,
            "provider_max_active_streams": provider_streams,
            "concurrency_lease_ttl_seconds": spec["ttl"],
            "stream_connect_timeout_seconds": 10,
            "stream_first_token_timeout_seconds": 60,
            "stream_idle_timeout_seconds": 120,
            "stream_max_duration_seconds": 600,
            "max_v1_request_body_bytes": int(spec["request_body_bytes"]),
            "max_v1_chat_request_body_bytes": int(spec["request_body_bytes"]),
            "max_v1_responses_request_body_bytes": int(spec["request_body_bytes"]),
            "long_output_stream_threshold_tokens": 8192,
            "max_non_stream_response_body_bytes": int(spec["non_stream_response_bytes"]),
            "stream_token_capture_max_bytes": int(spec["stream_capture_bytes"]),
            "max_logged_metadata_bytes": int(spec["logged_metadata_bytes"]),
            "max_logged_body_bytes": int(spec["logged_body_bytes"]),
            "async_request_logging": True,
            "max_candidate_count": spec["candidate_count"],
            "route_candidate_expand_count": 5,
            "route_candidate_cache_ttl_sec": route_ttl,
            "model_list_cache_ttl_sec": model_ttl,
            "provider_status_cache_ttl_sec": provider_ttl,
        }
        upstream_connections = max(active * 2, streams * 4, upstream_active_limit, 50)
        queue_workers = int(spec["queue_workers"])
        request_log_batch_size = max(50, min(500, active // 2))
        request_log_queue_size = max(10000, min(200000, active * 120))
        logging_event_batch_size = max(100, min(1000, active))
        redis_connections = max(
            100,
            min(
                5000,
                upstream_connections
                + workers * 20
                + queue_workers * 80
                + 100,
            ),
        )
        env_suggestions = {
            "WEB_CONCURRENCY": workers,
            "DB_POOL_SIZE": db_pool_size,
            "DB_MAX_OVERFLOW": db_max_overflow,
            "REDIS_MAX_CONNECTIONS": redis_connections,
            "REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS": 2,
            "REDIS_SOCKET_TIMEOUT_SECONDS": 2,
            "ENABLE_BACKGROUND_WORKERS": "false" if cpu_count <= 2 or memory_gb < 4 else "true",
            "ASYNC_REQUEST_LOG_ENABLED": "true",
            "REQUEST_LOG_QUEUE_WORKER_COUNT": queue_workers,
            "REQUEST_LOG_QUEUE_BATCH_SIZE": request_log_batch_size,
            "REQUEST_LOG_INGRESS_QUEUE_SIZE": request_log_queue_size,
            "LOGGING_EVENT_QUEUE_WORKER_COUNT": max(1, min(queue_workers, 3)),
            "LOGGING_EVENT_QUEUE_BATCH_SIZE": logging_event_batch_size,
            "TOKEN_FINALIZE_WORKER_COUNT": queue_workers,
            "TOKEN_FINALIZE_QUEUE_SIZE": max(10000, min(200000, active * 100)),
            "WORKER_THREADPOOL_TOKENS": max(20, min(160, cpu_count * 10)),
            "REQUEST_TIMEOUT_MS": 60000,
            "V1_REQUEST_BODY_IDLE_TIMEOUT_SECONDS": 15,
            "UPSTREAM_MAX_CONNECTIONS": upstream_connections,
            "UPSTREAM_MAX_KEEPALIVE_CONNECTIONS": max(20, min(upstream_connections, round(upstream_connections * 0.5))),
            "UPSTREAM_POOL_TIMEOUT_S": 10,
            "UPSTREAM_KEEPALIVE_EXPIRY_SECONDS": 30,
            "UPSTREAM_DNS_CACHE_TTL_SECONDS": 300,
            "UPSTREAM_REQUESTS_POOL_BLOCK": "true",
            "ROUTE_CAPACITY_PREFILTER_ENABLED": "true",
        }
        active_bottleneck = min(active_limit_candidates, key=active_limit_candidates.get)
        stream_bottleneck = min(stream_limit_candidates, key=stream_limit_candidates.get)
        explanations = [
            f"CPU 按 {cpu_count} 核和方案系数估算，非流式上限 {cpu_active_limit}、流式上限 {cpu_stream_limit}。",
            f"内存按总内存扣除 {round(float(spec['memory_reserve_ratio']) * 100)}% 系统余量，再扣除当前项目 RSS；单个非流式约 {spec['non_stream_memory_mb']} MB，单个流式约 {spec['stream_memory_mb']} MB。",
            f"数据库连接池建议 {db_pool_size}+{db_max_overflow}，热路径不在流式期间长期持有事务，所以流式按更高 DB 复用倍数估算。",
            f"出口带宽按 {round(network_bandwidth_mbps, 2)} Mbps × {round(bandwidth_utilization_ratio * 100)}% 安全利用率计算；非流式按 {round(non_stream_network_kbps)} kbps/请求、流式按 {round(stream_network_kbps)} kbps/连接预留。",
            "请求体、非流式响应体、流式捕获和日志正文上限随方案调整，用来限制大 payload 对内存、数据库和带宽的放大。",
        ]
        return {
            "id": spec["id"],
            "name": spec["name"],
            "summary": spec["summary"],
            "recommended": recommended,
            "precision": "host_resource_model",
            "estimated_capacity": {
                "non_stream_stable": max(1, round(active * 0.65)),
                "non_stream_peak": active,
                "stream_stable": max(1, round(streams * 0.75)),
                "stream_peak": streams,
            },
            "settings_patch": settings_patch,
            "settings_diff": {
                key: {"current": current_settings.get(key), "recommended": value}
                for key, value in settings_patch.items()
            },
            "env_suggestions": env_suggestions,
            "explanations": explanations,
            "calculation_basis": {
                "active_bottleneck": active_bottleneck,
                "stream_bottleneck": stream_bottleneck,
                "network_bandwidth_mbps": round(network_bandwidth_mbps, 2),
                "network_bandwidth_source": bandwidth_source,
                "usable_bandwidth_mbps": round(usable_bandwidth_mbps, 2),
                "bandwidth_utilization_ratio": bandwidth_utilization_ratio,
                "per_request_network_kbps": {
                    "non_stream": non_stream_network_kbps,
                    "stream": stream_network_kbps,
                },
                "usable_memory_bytes": usable_memory_bytes,
                "memory_reserve_bytes": reserve_bytes,
                "project_memory_rss_bytes": project_memory_rss_bytes or None,
                "per_request_memory_bytes": {
                    "non_stream": non_stream_memory_bytes,
                    "stream": stream_memory_bytes,
                    "mixed": mixed_request_memory_bytes,
                },
                "limits": {
                    "active": active_limit_candidates,
                    "stream": stream_limit_candidates,
                },
                "current_runtime_limits": {
                    "upstream_max_connections": runtime_snapshot.get("upstream_max_connections"),
                    "db_total_connections": runtime_snapshot.get("db_total_connections"),
                    "web_concurrency": runtime_snapshot.get("web_concurrency"),
                },
            },
        }

    @staticmethod
    def _bounded_int(value: float, *, minimum: int, maximum: int) -> int:
        return int(max(minimum, min(maximum, round(value))))

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

    @staticmethod
    def _safe_cached_snapshot(
        db: Session,
        section_errors: dict[str, str],
        section_name: str,
        cache_key: str,
        loader: Any,
        fallback_factory: Any,
    ) -> Any:
        cached = CacheService.get(cache_key)
        if cached is not None:
            return cached
        try:
            value = loader()
            return CacheService.set(cache_key, value, ttl_seconds=SystemMetricsService.DB_SECTION_CACHE_SECONDS)
        except Exception as exc:
            if db is not None:
                db.rollback()
            section_errors[section_name] = str(exc)
            return fallback_factory()

    @classmethod
    def write_monitoring_alerts(cls, db: Session, metrics: dict[str, Any]) -> None:
        active_events = cls._build_monitoring_alert_events(metrics)
        changed = cls.apply_monitoring_alert_actions(db, active_events, auto_commit=False)
        active_keys = set(active_events)
        existing_items = cls._load_relevant_monitoring_alerts(db, active_keys)
        existing_by_key = {item.alert_key: item for item in existing_items}
        now = now_beijing()
        for alert_key, payload in active_events.items():
            item = existing_by_key.get(alert_key)
            if item is None:
                item = AlertEvent(
                    alert_key=alert_key,
                    alert_type=payload["alert_type"],
                    first_seen_at=now,
                )
                db.add(item)
                changed = True
            alert_payload = cls._monitoring_alert_payload_with_preserved_fields(item, payload["payload"])
            payload_json = dumps_json(alert_payload)
            material_changed = False
            for field, value in (
                ("severity", payload["severity"]),
                ("title", payload["title"]),
                ("message", payload["message"]),
                ("payload_json", payload_json),
                ("status", "active"),
            ):
                if getattr(item, field, None) == value:
                    continue
                setattr(item, field, value)
                material_changed = True
            if item.resolved_at is not None:
                item.resolved_at = None
                material_changed = True
            should_refresh_last_seen = (
                item.last_seen_at is None
                or material_changed
                or (now - item.last_seen_at).total_seconds() >= cls.MONITORING_ALERT_LAST_SEEN_REFRESH_SECONDS
            )
            if should_refresh_last_seen and item.last_seen_at != now:
                item.last_seen_at = now
                material_changed = True
            changed = changed or material_changed
        changed = cls._resolve_stale_monitoring_alerts(db, active_keys, now=now) or changed
        if changed:
            db.commit()

    @staticmethod
    def _monitoring_alert_payload_with_preserved_fields(item: AlertEvent | None, payload: Any) -> Any:
        if item is None or not isinstance(payload, dict):
            return payload
        existing = loads_json(getattr(item, "payload_json", None), {})
        if not isinstance(existing, dict):
            return payload
        merged = dict(payload)
        for key in (
            "isolated_at",
            "pre_isolation_snapshot",
            "isolation_previous_status",
            "isolation_previous_models",
        ):
            if key not in merged and key in existing:
                merged[key] = existing[key]
        return merged

    @classmethod
    def _load_relevant_monitoring_alerts(cls, db: Session, active_keys: set[str]) -> list[AlertEvent]:
        if not active_keys:
            return []
        return list(db.scalars(select(AlertEvent).where(AlertEvent.alert_key.in_(active_keys))))

    @classmethod
    def _resolve_stale_monitoring_alerts(cls, db: Session, active_keys: set[str], *, now: datetime) -> bool:
        changed = False
        for _ in range(cls.MONITORING_ALERT_RESOLVE_MAX_BATCHES):
            query = select(AlertEvent).where(
                AlertEvent.alert_key.like("monitoring:%"),
                AlertEvent.status != "resolved",
            )
            if active_keys:
                query = query.where(AlertEvent.alert_key.not_in(active_keys))
            stale_items = list(
                db.scalars(
                    query.order_by(AlertEvent.id.asc()).limit(cls.MONITORING_ALERT_RESOLVE_BATCH_SIZE)
                )
            )
            if not stale_items:
                break
            for item in stale_items:
                item.status = "resolved"
                item.resolved_at = now
                changed = True
            if len(stale_items) < cls.MONITORING_ALERT_RESOLVE_BATCH_SIZE:
                break
        return changed

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
        changed = cls._apply_monitoring_actions(db, active_events, now=now_beijing())
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
        is_new_isolation = provider.content_integrity_status != "blocked"
        if is_new_isolation:
            payload.setdefault("pre_isolation_snapshot", cls._content_guard_isolation_snapshot(provider))
            payload.setdefault("isolated_at", now.isoformat())
        updates = {
            "content_integrity_status": "blocked",
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
            }
            for field, value in model_updates.items():
                if getattr(provider_model, field) == value:
                    continue
                setattr(provider_model, field, value)
                changed = True
        payload["auto_isolated"] = True
        payload["isolation_status"] = "blocked" if (is_new_isolation or changed) else "already_blocked"
        event["message"] = (
            f"最近 {payload.get('window_minutes', cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_WINDOW_MINUTES)} 分钟"
            f"内容高风险命中 {high_risk_count} 次，已自动隔离并从路由候选排除"
        )
        return changed

    @staticmethod
    def _content_guard_isolation_snapshot(provider: Provider) -> dict[str, Any]:
        def dt(value: Any) -> str | None:
            return value.isoformat() if isinstance(value, datetime) else None

        models = list(getattr(provider, "provider_models", []) or [])
        sampled_models = models[:SystemMetricsService.CONTENT_GUARD_ISOLATION_SNAPSHOT_MODEL_LIMIT]

        return {
            "provider": {
                "id": provider.id,
                "name": provider.name,
                "trust_level": getattr(provider, "trust_level", None),
                "content_integrity_status": getattr(provider, "content_integrity_status", None),
                "content_integrity_score": getattr(provider, "content_integrity_score", None),
                "circuit_state": getattr(provider, "circuit_state", None),
                "circuit_opened_at": dt(getattr(provider, "circuit_opened_at", None)),
                "last_content_violation_at": dt(getattr(provider, "last_content_violation_at", None)),
            },
            "models": [
                {
                    "id": model.id,
                    "model_name": model.model_name,
                    "enabled": getattr(model, "enabled", None),
                    "health_status": getattr(model, "health_status", None),
                    "content_integrity_status": getattr(model, "content_integrity_status", None),
                    "circuit_state": getattr(model, "circuit_state", None),
                    "circuit_opened_at": dt(getattr(model, "circuit_opened_at", None)),
                }
                for model in sampled_models
            ],
            "models_total": len(models),
            "models_snapshot_limit": SystemMetricsService.CONTENT_GUARD_ISOLATION_SNAPSHOT_MODEL_LIMIT,
            "models_truncated": len(models) > SystemMetricsService.CONTENT_GUARD_ISOLATION_SNAPSHOT_MODEL_LIMIT,
        }

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
                "request_log_queue": cls._empty_queue_snapshot(),
                "logging_event_queue": cls._empty_queue_snapshot(),
                "error": "REDIS_URL is empty",
            }
        started = time.perf_counter()
        try:
            client = RedisService.create_sync_client()
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
                    "token_finalize_backlog": None,
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
                "request_log_queue": cls._empty_queue_snapshot(),
                "logging_event_queue": cls._empty_queue_snapshot(),
                "error": str(exc),
            }

    @staticmethod
    def _empty_queue_snapshot() -> dict[str, int | float | None]:
        return {
            "queued": None,
            "processing": None,
            "dead_letter": None,
            "failure_count": None,
            "total": None,
            "oldest_queued_wait_ms": None,
            "oldest_processing_wait_ms": None,
        }

    @classmethod
    def _request_log_queue_snapshot(cls, client: Redis) -> dict[str, int | float | None]:
        try:
            queued = int(client.llen(RequestLogQueueService.QUEUE_KEY) or 0)
            processing = int(client.llen(RequestLogQueueService.PROCESSING_KEY) or 0)
            dead_letter = int(client.llen(RequestLogQueueService.DEAD_LETTER_KEY) or 0)
            failure_count = int(client.get(RequestLogQueueService.FAILURE_COUNT_KEY) or 0)
            return {
                "queued": queued,
                "processing": processing,
                "dead_letter": dead_letter,
                "failure_count": failure_count,
                "total": queued + processing,
                "oldest_queued_wait_ms": cls._queue_oldest_wait_ms(client, RequestLogQueueService.QUEUE_KEY),
                "oldest_processing_wait_ms": cls._queue_oldest_wait_ms(client, RequestLogQueueService.PROCESSING_KEY),
            }
        except Exception:
            return cls._empty_queue_snapshot()

    @classmethod
    def _logging_event_queue_snapshot(cls, client: Redis) -> dict[str, int | float | None]:
        try:
            queued = int(client.llen(LoggingQueue.QUEUE_KEY) or 0)
            processing = int(client.llen(LoggingQueue.PROCESSING_KEY) or 0)
            dead_letter = int(client.llen(LoggingQueue.DEAD_LETTER_KEY) or 0)
            failure_count = int(client.get(LoggingQueue.FAILURE_COUNT_KEY) or 0)
            return {
                "queued": queued,
                "processing": processing,
                "dead_letter": dead_letter,
                "failure_count": failure_count,
                "total": queued + processing,
                "oldest_queued_wait_ms": cls._queue_oldest_wait_ms(client, LoggingQueue.QUEUE_KEY),
                "oldest_processing_wait_ms": cls._queue_oldest_wait_ms(client, LoggingQueue.PROCESSING_KEY),
            }
        except Exception:
            return cls._empty_queue_snapshot()

    @staticmethod
    def _queue_oldest_wait_ms(client: Redis, queue_key: str) -> float | None:
        raw_item = client.lindex(queue_key, -1)
        if not raw_item:
            return None
        payload = loads_json(raw_item, {})
        if not isinstance(payload, dict):
            return None
        kwargs = payload.get("kwargs")
        if isinstance(kwargs, dict):
            enqueued_at = kwargs.get("_queue_enqueued_at")
        else:
            enqueued_at = payload.get("_queue_enqueued_at")
        try:
            return round(max(0.0, (time.time() - float(enqueued_at)) * 1000), 2)
        except (TypeError, ValueError):
            return None

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
    def _scheduler_job_states(client: Redis) -> dict[str, dict[str, str]]:
        states: dict[str, dict[str, str]] = {}
        scanned = 0
        for key in client.scan_iter(match="scheduler:job:*:state", count=50):
            scanned += 1
            if scanned > 100:
                states["_truncated"] = {"reason": "scheduler job state scan reached limit"}
                break
            job_name = str(key).removeprefix("scheduler:job:").removesuffix(":state")
            raw_state = client.hgetall(key)
            states[job_name] = {str(item_key): str(item_value) for item_key, item_value in raw_state.items()}
        return states

    @staticmethod
    def _runtime_snapshot() -> dict[str, Any]:
        return {
            "worker_active_requests": RuntimeStateService.current_active_requests(),
            "worker_peak_active_requests": RuntimeStateService.peak_active_requests(),
            "event_loop": RuntimeStateService.event_loop_snapshot(),
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
            "cpu_times_percent": {},
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "load_per_cpu": None,
            "memory": {
                "total_bytes": None,
                "available_bytes": None,
                "used_bytes": None,
                "percent": None,
            },
            "swap": {
                "total_bytes": None,
                "used_bytes": None,
                "percent": None,
            },
            "disk": cls._empty_disk_snapshot(project_root),
            "network": cls._empty_network_snapshot(),
            "cgroup": cls._cgroup_snapshot(),
            "process": {
                "pid": os.getpid(),
                "cpu_percent": None,
                "memory_rss_bytes": None,
                "memory_vms_bytes": None,
                "memory_percent": None,
                "thread_count": None,
                "open_file_count": None,
                "connection_count": None,
                "connection_status_counts": {},
                "fd_or_handle_count": None,
                "fd_limit_soft": None,
                "fd_limit_hard": None,
                "fd_usage_ratio": None,
                "started_at": timestamp_to_beijing(PROCESS_STARTED_AT).isoformat(),
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
            swap = psutil.swap_memory()
            process = psutil.Process(os.getpid())
            with process.oneshot():
                process_memory = process.memory_info()
                open_file_count = cls._safe_open_file_count(process)
                connection_counts = cls._safe_process_connection_counts(process)
                fd_info = cls._safe_fd_info(process)
                create_time = process.create_time()
                snapshot["process"].update(
                    {
                        "cpu_percent": round(process.cpu_percent(interval=None), 2),
                        "memory_rss_bytes": process_memory.rss,
                        "memory_vms_bytes": process_memory.vms,
                        "memory_percent": round(process.memory_percent(), 2),
                        "thread_count": process.num_threads(),
                        "open_file_count": open_file_count,
                        "connection_count": sum(connection_counts.values()) if connection_counts else 0,
                        "connection_status_counts": connection_counts,
                        **fd_info,
                        "started_at": timestamp_to_beijing(create_time).isoformat(),
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
                    "cpu_times_percent": cls._cpu_times_percent_snapshot(),
                    "load_per_cpu": cls._load_per_cpu(snapshot.get("load_average"), os.cpu_count()),
                    "memory": {
                        "total_bytes": memory.total,
                        "available_bytes": memory.available,
                        "used_bytes": memory.used,
                        "percent": memory.percent,
                    },
                    "swap": {
                        "total_bytes": swap.total,
                        "used_bytes": swap.used,
                        "percent": swap.percent,
                    },
                    "disk": cls._disk_snapshot(project_root),
                    "network": cls._network_snapshot(),
                }
            )
            return snapshot
        except Exception as exc:
            snapshot["available"] = False
            snapshot["error"] = str(exc)
            return snapshot

    @classmethod
    def _empty_disk_snapshot(cls, project_root: str) -> dict[str, Any]:
        return {
            "scope": "partition_metrics_only",
            "project_partition": {
                "path": project_root,
                "total_bytes": None,
                "used_bytes": None,
                "free_bytes": None,
                "percent": None,
            },
            "io": {
                "read_bytes": None,
                "write_bytes": None,
                "read_count": None,
                "write_count": None,
                "busy_time_ms": None,
            },
        }

    @classmethod
    def _disk_snapshot(cls, project_root: str) -> dict[str, Any]:
        snapshot = cls._empty_disk_snapshot(project_root)
        if psutil is None:
            return snapshot
        try:
            usage = psutil.disk_usage(project_root)
            snapshot["project_partition"] = {
                "path": project_root,
                "scope": "host_partition",
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent": usage.percent,
                "note": "分区级指标，不代表项目目录递归占用。",
            }
        except Exception as exc:
            snapshot["project_partition"]["error"] = str(exc)
        try:
            counters = psutil.disk_io_counters()
            if counters is not None:
                snapshot["io"] = {
                    "read_bytes": getattr(counters, "read_bytes", None),
                    "write_bytes": getattr(counters, "write_bytes", None),
                    "read_count": getattr(counters, "read_count", None),
                    "write_count": getattr(counters, "write_count", None),
                    "busy_time_ms": getattr(counters, "busy_time", None),
                }
        except Exception as exc:
            snapshot["io"]["error"] = str(exc)
        return snapshot

    @staticmethod
    def _empty_network_snapshot() -> dict[str, Any]:
        return {
            "io": {
                "bytes_sent": None,
                "bytes_recv": None,
                "sent_mbps": None,
                "recv_mbps": None,
                "total_mbps": None,
                "sample_seconds": None,
                "packets_sent": None,
                "packets_recv": None,
                "errin": None,
                "errout": None,
                "dropin": None,
                "dropout": None,
            },
            "tcp_status_counts": {},
            "tcp_connection_count": None,
        }

    @classmethod
    def _network_snapshot(cls) -> dict[str, Any]:
        snapshot = cls._empty_network_snapshot()
        if psutil is None:
            return snapshot
        try:
            counters = psutil.net_io_counters()
            if counters is not None:
                bytes_sent = getattr(counters, "bytes_sent", None)
                bytes_recv = getattr(counters, "bytes_recv", None)
                rate_snapshot = cls._network_rate_snapshot(bytes_sent=bytes_sent, bytes_recv=bytes_recv)
                snapshot["io"] = {
                    "bytes_sent": bytes_sent,
                    "bytes_recv": bytes_recv,
                    **rate_snapshot,
                    "packets_sent": getattr(counters, "packets_sent", None),
                    "packets_recv": getattr(counters, "packets_recv", None),
                    "errin": getattr(counters, "errin", None),
                    "errout": getattr(counters, "errout", None),
                    "dropin": getattr(counters, "dropin", None),
                    "dropout": getattr(counters, "dropout", None),
                }
        except Exception as exc:
            snapshot["io"]["error"] = str(exc)
        try:
            counts = cls._tcp_status_counts()
            snapshot["tcp_status_counts"] = dict(sorted(counts.items()))
            snapshot["tcp_connection_count"] = sum(counts.values())
        except Exception as exc:
            snapshot["tcp_status_error"] = str(exc)
        return snapshot

    @classmethod
    def _network_rate_snapshot(cls, *, bytes_sent: Any, bytes_recv: Any) -> dict[str, Any]:
        now = time.time()
        result = {
            "sent_mbps": None,
            "recv_mbps": None,
            "total_mbps": None,
            "sample_seconds": None,
        }
        previous_sent = cls._network_io_cache.get("bytes_sent")
        previous_recv = cls._network_io_cache.get("bytes_recv")
        previous_checked_at = float(cls._network_io_cache.get("checked_at") or 0.0)
        cls._network_io_cache.update(
            {
                "checked_at": now,
                "bytes_sent": bytes_sent,
                "bytes_recv": bytes_recv,
            }
        )
        if not all(isinstance(item, (int, float)) for item in (bytes_sent, bytes_recv, previous_sent, previous_recv)):
            return result
        elapsed = now - previous_checked_at
        if elapsed <= 0:
            return result
        sent_delta = max(0.0, float(bytes_sent) - float(previous_sent))
        recv_delta = max(0.0, float(bytes_recv) - float(previous_recv))
        sent_mbps = sent_delta * 8 / elapsed / 1_000_000
        recv_mbps = recv_delta * 8 / elapsed / 1_000_000
        result.update(
            {
                "sent_mbps": round(sent_mbps, 3),
                "recv_mbps": round(recv_mbps, 3),
                "total_mbps": round(sent_mbps + recv_mbps, 3),
                "sample_seconds": round(elapsed, 2),
            }
        )
        return result

    @classmethod
    def _tcp_status_counts(cls) -> Counter[str]:
        proc_counts = cls._tcp_status_counts_from_proc()
        if proc_counts:
            return proc_counts
        if platform.system().lower().startswith("win"):
            return Counter()
        if psutil is None:
            return Counter()
        counts: Counter[str] = Counter()
        for conn in psutil.net_connections(kind="tcp"):
            counts[str(getattr(conn, "status", "") or "UNKNOWN").upper()] += 1
        return counts

    @staticmethod
    def _tcp_status_counts_from_proc() -> Counter[str]:
        state_names = {
            "01": "ESTABLISHED",
            "02": "SYN_SENT",
            "03": "SYN_RECV",
            "04": "FIN_WAIT1",
            "05": "FIN_WAIT2",
            "06": "TIME_WAIT",
            "07": "CLOSE",
            "08": "CLOSE_WAIT",
            "09": "LAST_ACK",
            "0A": "LISTEN",
            "0B": "CLOSING",
        }
        counts: Counter[str] = Counter()
        for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            text = SystemMetricsService._read_text_file(path)
            if not text:
                continue
            for line in text.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                state = parts[3].upper()
                counts[state_names.get(state, state)] += 1
        return counts

    @staticmethod
    def _cpu_times_percent_snapshot() -> dict[str, float]:
        if psutil is None:
            return {}
        try:
            times = psutil.cpu_times_percent(interval=None)
        except Exception:
            return {}
        return {
            key: round(float(value), 2)
            for key, value in times._asdict().items()
            if isinstance(value, (int, float))
        }

    @staticmethod
    def _load_per_cpu(load_average: Any, cpu_count: int | None) -> float | None:
        if not load_average or not cpu_count:
            return None
        try:
            return round(float(load_average[0]) / max(1, int(cpu_count)), 4)
        except (TypeError, ValueError, IndexError):
            return None

    @staticmethod
    def _safe_open_file_count(process: Any) -> int | None:
        try:
            return len(process.open_files())
        except Exception:
            return None

    @staticmethod
    def _safe_process_connection_counts(process: Any) -> dict[str, int]:
        if platform.system().lower().startswith("win"):
            return {}
        try:
            if hasattr(process, "net_connections"):
                connections = process.net_connections(kind="tcp")
            else:
                connections = process.connections(kind="tcp")
        except Exception:
            return {}
        counts = Counter(str(getattr(conn, "status", "") or "UNKNOWN").upper() for conn in connections)
        return dict(sorted(counts.items()))

    @staticmethod
    def _safe_fd_info(process: Any) -> dict[str, Any]:
        used: int | None = None
        if hasattr(process, "num_fds"):
            try:
                used = int(process.num_fds())
            except Exception:
                used = None
        elif hasattr(process, "num_handles"):
            try:
                used = int(process.num_handles())
            except Exception:
                used = None
        soft: int | None = None
        hard: int | None = None
        try:
            import resource

            raw_soft, raw_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            soft = int(raw_soft) if raw_soft >= 0 else None
            hard = int(raw_hard) if raw_hard >= 0 else None
        except Exception:
            soft = None
            hard = None
        ratio = round(used / soft, 6) if used is not None and soft and soft > 0 else None
        return {
            "fd_or_handle_count": used,
            "fd_limit_soft": soft,
            "fd_limit_hard": hard,
            "fd_usage_ratio": ratio,
        }

    @staticmethod
    def _cgroup_snapshot() -> dict[str, Any]:
        root = Path("/sys/fs/cgroup")
        snapshot: dict[str, Any] = {
            "available": root.exists(),
            "version": None,
            "memory": {"current_bytes": None, "max_bytes": None, "usage_percent": None},
            "cpu": {"max": None, "quota_cores": None, "stat": {}},
            "pids": {"current": None, "max": None, "usage_percent": None},
        }
        if not root.exists():
            return snapshot
        snapshot["version"] = "v2" if (root / "cgroup.controllers").exists() else "v1"
        memory_current = SystemMetricsService._read_int_file(root / "memory.current")
        memory_max = SystemMetricsService._read_cgroup_max(root / "memory.max")
        if memory_current is None:
            memory_current = SystemMetricsService._read_int_file(root / "memory" / "memory.usage_in_bytes")
        if memory_max is None:
            memory_max = SystemMetricsService._read_cgroup_max(root / "memory" / "memory.limit_in_bytes")
        snapshot["memory"] = {
            "current_bytes": memory_current,
            "max_bytes": memory_max,
            "usage_percent": round(memory_current / memory_max * 100, 2) if memory_current is not None and memory_max else None,
        }
        cpu_max_text = SystemMetricsService._read_text_file(root / "cpu.max")
        quota_cores = None
        if cpu_max_text:
            parts = cpu_max_text.split()
            if len(parts) >= 2 and parts[0] != "max":
                try:
                    quota_cores = round(int(parts[0]) / max(1, int(parts[1])), 4)
                except ValueError:
                    quota_cores = None
        else:
            quota = SystemMetricsService._read_int_file(root / "cpu" / "cpu.cfs_quota_us")
            period = SystemMetricsService._read_int_file(root / "cpu" / "cpu.cfs_period_us")
            if quota is not None and period and quota > 0:
                quota_cores = round(quota / period, 4)
        snapshot["cpu"] = {
            "max": cpu_max_text,
            "quota_cores": quota_cores,
            "stat": SystemMetricsService._read_key_value_file(root / "cpu.stat"),
        }
        pids_current = SystemMetricsService._read_int_file(root / "pids.current")
        pids_max = SystemMetricsService._read_cgroup_max(root / "pids.max")
        snapshot["pids"] = {
            "current": pids_current,
            "max": pids_max,
            "usage_percent": round(pids_current / pids_max * 100, 2) if pids_current is not None and pids_max else None,
        }
        return snapshot

    @staticmethod
    def _read_text_file(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return None

    @staticmethod
    def _read_int_file(path: Path) -> int | None:
        value = SystemMetricsService._read_text_file(path)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    @staticmethod
    def _read_cgroup_max(path: Path) -> int | None:
        value = SystemMetricsService._read_text_file(path)
        if value in (None, "", "max"):
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _read_key_value_file(path: Path) -> dict[str, int]:
        text = SystemMetricsService._read_text_file(path)
        if not text:
            return {}
        result: dict[str, int] = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                continue
        return result

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
            "checked_at": timestamp_to_beijing(now).isoformat(),
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
                        "started_at": timestamp_to_beijing(create_time).isoformat(),
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
            "oldest_started_at": timestamp_to_beijing(oldest_create_time).isoformat() if oldest_create_time else None,
            "uptime_seconds": round(now - oldest_create_time, 2) if oldest_create_time else None,
            "processes": process_items[:50],
            "cache_seconds": cls.PROJECT_PROCESS_CACHE_SECONDS,
            "cache_age_seconds": 0.0,
            "checked_at": timestamp_to_beijing(now).isoformat(),
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
        if platform.system().lower().startswith("win"):
            return list(related.values())
        scanned = 0
        for candidate in psutil.process_iter(["pid", "name"]):
            scanned += 1
            if scanned > cls.PROCESS_SCAN_LIMIT:
                break
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
        since = now_beijing() - timedelta(minutes=window_minutes)
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
        latency_sample_rows = db.execute(
            select(
                RequestLog.latency_ms,
                RequestLog.is_stream,
                RequestLog.first_token_latency_ms,
            )
            .where(
                RequestLog.created_at >= since,
                RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
                or_(
                    RequestLog.latency_ms.is_not(None),
                    RequestLog.first_token_latency_ms.is_not(None),
                ),
            )
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
            .limit(cls.METRIC_PERCENTILE_SAMPLE_LIMIT)
        )
        latencies: list[Any] = []
        stream_latencies: list[Any] = []
        non_stream_latencies: list[Any] = []
        stream_ttfb_latencies: list[Any] = []
        for latency_ms, is_stream, first_token_latency_ms in latency_sample_rows:
            if latency_ms is not None:
                latencies.append(latency_ms)
                if is_stream is True:
                    stream_latencies.append(latency_ms)
                elif is_stream is False:
                    non_stream_latencies.append(latency_ms)
            if is_stream is True and first_token_latency_ms is not None:
                stream_ttfb_latencies.append(first_token_latency_ms)
        terminal_rows = db.execute(
            select(RequestLog.status_code, RequestLog.error_code)
            .where(
                RequestLog.created_at >= since,
                RequestLog.log_type.in_(cls.TRAFFIC_LOG_TYPES),
                RequestLog.success.is_(False),
            )
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
            .limit(cls.METRIC_PERCENTILE_SAMPLE_LIMIT)
        )
        terminal_breakdown = cls._terminal_breakdown(terminal_rows)
        stream_count = int(row.stream_requests or 0)
        non_stream_count = max(0, total - stream_count)
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
            "latency_by_mode": {
                "stream": {
                    "request_count": stream_count,
                    "p50_latency_ms": cls._percentile(stream_latencies, 50),
                    "p95_latency_ms": cls._percentile(stream_latencies, 95),
                    "p99_latency_ms": cls._percentile(stream_latencies, 99),
                    "first_token_p50_ms": cls._percentile(stream_ttfb_latencies, 50),
                    "first_token_p95_ms": cls._percentile(stream_ttfb_latencies, 95),
                    "first_token_p99_ms": cls._percentile(stream_ttfb_latencies, 99),
                },
                "non_stream": {
                    "request_count": non_stream_count,
                    "p50_latency_ms": cls._percentile(non_stream_latencies, 50),
                    "p95_latency_ms": cls._percentile(non_stream_latencies, 95),
                    "p99_latency_ms": cls._percentile(non_stream_latencies, 99),
                },
            },
            "terminal_breakdown": terminal_breakdown,
            "avg_first_token_latency_ms": round(float(row.avg_first_token_latency_ms), 2)
            if row.avg_first_token_latency_ms is not None
            else None,
            "stream_requests": int(row.stream_requests or 0),
            "image_requests": int(row.image_requests or 0),
        }

    @classmethod
    def _terminal_breakdown(cls, rows: Any) -> dict[str, int]:
        breakdown = {
            "client_cancelled": 0,
            "upstream_failed": 0,
            "system_limited": 0,
            "other_failed": 0,
        }
        for status_code, error_code in rows:
            category = cls._terminal_category(status_code, error_code)
            breakdown[category] += 1
        return breakdown

    @staticmethod
    def _terminal_category(status_code: Any, error_code: Any) -> str:
        code = str(error_code or "")
        try:
            status_value = int(status_code or 0)
        except (TypeError, ValueError):
            status_value = 0
        if status_value == 499 or code == "client_cancelled":
            return "client_cancelled"
        if status_value == 429 or code in {
            "concurrency_limit_exceeded",
            "stream_concurrency_exceeded",
            "provider_concurrency_limit_exceeded",
            "provider_capacity_exceeded",
            "provider_active_request_limit_exceeded",
            "provider_active_stream_limit_exceeded",
            "provider_qps_limit_exceeded",
            "provider_rpm_limit_exceeded",
            "request_tokens_exceeded",
            "model_input_tokens_exceeded",
            "request_body_too_large",
            "insufficient_balance",
            "insufficient_balance_for_estimated_request",
        }:
            return "system_limited"
        if code.startswith("upstream_") or code in {
            "route_unavailable",
            "route_exhausted",
            "provider_capacity_unavailable",
            "provider_capacity_service_unavailable",
            "invalid_upstream_response",
            "empty_stream_response",
            "stream_first_token_timeout",
            "stream_idle_timeout",
            "stream_max_duration_exceeded",
        }:
            return "upstream_failed"
        return "other_failed"

    @classmethod
    def _content_guard_snapshot(cls, db: Session, *, window_minutes: int) -> dict[str, Any]:
        setting = SettingService.get_or_create(db)
        if not bool(getattr(setting, "content_guard_enabled", True)):
            return cls._empty_content_guard(enabled=False)
        since = now_beijing() - timedelta(minutes=window_minutes)
        high_risk_since = now_beijing() - timedelta(minutes=cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_WINDOW_MINUTES)
        event_provider_join = RequestContentGuardEvent.__table__.outerjoin(
            RequestLog.__table__,
            RequestContentGuardEvent.request_log_id == RequestLog.id,
        )
        row = db.execute(
            select(
                func.count(RequestContentGuardEvent.id).label("total_requests"),
                func.sum(case((RequestContentGuardEvent.guard_result == "block", 1), else_=0)).label("block_count"),
                func.sum(case((RequestContentGuardEvent.guard_result == "review", 1), else_=0)).label("review_count"),
                func.sum(case((RequestContentGuardEvent.risk_level == "high", 1), else_=0)).label("high_risk_count"),
            )
            .select_from(event_provider_join)
            .where(
                RequestContentGuardEvent.created_at >= since,
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
                func.count(RequestContentGuardEvent.id).label("total_requests"),
                func.sum(
                    case(
                        (
                            or_(
                                RequestContentGuardEvent.guard_result == "block",
                                RequestContentGuardEvent.risk_level == "high",
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("high_risk_count"),
                func.sum(case((RequestContentGuardEvent.guard_result == "review", 1), else_=0)).label("review_count"),
            )
            .select_from(event_provider_join)
            .where(
                RequestContentGuardEvent.created_at >= since,
                RequestLog.provider_id.is_not(None),
            )
            .group_by(RequestLog.provider_id, RequestLog.provider_name)
            .order_by(func.count(RequestContentGuardEvent.id).desc())
            .limit(cls.PROVIDER_METRIC_LIMIT)
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
                    "violation_rate": round((high_risk / total) * 100, 2) if total else 0.0,
                    "review_rate": round((review / total) * 100, 2) if total else 0.0,
                }
            )
        high_risk_rows = db.execute(
            select(
                RequestLog.provider_id,
                RequestLog.provider_name,
                func.count(RequestContentGuardEvent.id).label("high_risk_count"),
            )
            .select_from(event_provider_join)
            .where(
                RequestContentGuardEvent.created_at >= high_risk_since,
                RequestLog.provider_id.is_not(None),
                RequestContentGuardEvent.guard_result == "block",
            )
            .group_by(RequestLog.provider_id, RequestLog.provider_name)
            .order_by(func.count(RequestContentGuardEvent.id).desc())
            .limit(cls.PROVIDER_METRIC_LIMIT)
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
        since = now_beijing() - timedelta(minutes=window_minutes)
        providers = list(
            db.scalars(
                select(Provider)
                .order_by(Provider.id.asc())
                .limit(cls.PROVIDER_METRIC_LIMIT)
            )
        )
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
        now = time.time()
        cached_snapshot = cls._background_snapshot_cache.get("snapshot")
        checked_at = float(cls._background_snapshot_cache.get("checked_at") or 0.0)
        if isinstance(cached_snapshot, dict) and now - checked_at < cls.BACKGROUND_SNAPSHOT_CACHE_SECONDS:
            return dict(cached_snapshot)
        scheduler_jobs = len(scheduler.get_jobs()) if scheduler.running else 0
        pending_finalize = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._pending_token_billing_finalize_expr(),
                )
            ) or 0
        )
        billing_failed = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._pending_token_billing_finalize_expr(),
                    RequestLog.billing_error.is_not(None),
                )
            ) or 0
        )
        token_failed = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._pending_token_billing_finalize_expr(),
                    RequestLog.token_finalize_error.is_not(None),
                )
            ) or 0
        )
        token_dead_letter = TokenUsageService.finalize_dead_letter_snapshot()
        snapshot = {
            "scheduler_running": scheduler.running,
            "scheduler_jobs": scheduler_jobs,
            "pending_finalize_logs": pending_finalize,
            "billing_failed_logs": billing_failed,
            "token_failed_logs": token_failed,
            "token_finalize_dead_letter": token_dead_letter.get("dead_letter"),
            "token_finalize_failure_count": token_dead_letter.get("failure_count"),
            "recent_failed_jobs": cls._recent_failed_background_jobs(db),
        }
        cls._background_snapshot_cache = {"snapshot": dict(snapshot), "checked_at": now}
        return snapshot

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
            "latency_by_mode": {
                "stream": {
                    "request_count": 0,
                    "p50_latency_ms": None,
                    "p95_latency_ms": None,
                    "p99_latency_ms": None,
                    "first_token_p50_ms": None,
                    "first_token_p95_ms": None,
                    "first_token_p99_ms": None,
                },
                "non_stream": {
                    "request_count": 0,
                    "p50_latency_ms": None,
                    "p95_latency_ms": None,
                    "p99_latency_ms": None,
                },
            },
            "terminal_breakdown": {
                "client_cancelled": 0,
                "upstream_failed": 0,
                "system_limited": 0,
                "other_failed": 0,
            },
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
            "token_finalize_dead_letter": None,
            "token_finalize_failure_count": None,
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
        host = metrics.get("host", {})
        runtime = metrics.get("runtime", {})
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
        terminal_breakdown = traffic.get("terminal_breakdown") if isinstance(traffic.get("terminal_breakdown"), dict) else {}
        client_cancelled = int(terminal_breakdown.get("client_cancelled") or 0)
        if client_cancelled >= cls.CLIENT_CANCEL_WARNING_THRESHOLD:
            events["monitoring:client_cancelled_spike"] = cls._event(
                "monitoring:client_cancelled_spike",
                "failure_rate",
                "warning",
                "客户端取消请求突增",
                f"最近 {metrics.get('window_minutes')} 分钟客户端取消 {client_cancelled} 次",
                terminal_breakdown,
            )
        queue_wait_ms = cls._max_queue_wait_ms(redis_snapshot)
        if queue_wait_ms is not None and queue_wait_ms >= cls.QUEUE_WAIT_WARNING_MS:
            events["monitoring:queue_wait_high"] = cls._event(
                "monitoring:queue_wait_high",
                "system",
                "warning",
                "后台队列等待时间过高",
                f"最老队列项等待 {queue_wait_ms} ms，阈值 {cls.QUEUE_WAIT_WARNING_MS} ms",
                {
                    "queue_wait_ms": queue_wait_ms,
                    "request_log_queue": redis_snapshot.get("request_log_queue"),
                    "logging_event_queue": redis_snapshot.get("logging_event_queue"),
                },
            )
        event_loop = runtime.get("event_loop") if isinstance(runtime.get("event_loop"), dict) else {}
        latest_loop_delay = cls._as_float(event_loop.get("latest_delay_ms"))
        max_loop_delay = cls._as_float(event_loop.get("recent_max_delay_ms"))
        if max_loop_delay is None:
            max_loop_delay = cls._as_float(event_loop.get("max_delay_ms"))
        loop_delay = max(
            value
            for value in (latest_loop_delay, max_loop_delay, 0.0)
            if value is not None
        )
        if loop_delay >= cls.EVENT_LOOP_DELAY_DANGER_MS:
            level = "danger"
        elif loop_delay >= cls.EVENT_LOOP_DELAY_WARNING_MS:
            level = "warning"
        else:
            level = None
        if level is not None:
            events["monitoring:event_loop_delay_high"] = cls._event(
                "monitoring:event_loop_delay_high",
                "system",
                level,
                "事件循环延迟过高",
                f"当前 worker 最近 {event_loop.get('window_seconds') or 60} 秒事件循环最大延迟 {loop_delay} ms",
                event_loop,
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
                    f"最近 {provider.get('window_minutes', cls.CONTENT_GUARD_HIGH_RISK_PROVIDER_WINDOW_MINUTES)} 分钟内容高风险命中 {provider.get('high_risk_count')} 次，已达到自动隔离阈值",
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
        events.update(cls._build_host_resource_alert_events(host))
        return events

    @staticmethod
    def _max_queue_wait_ms(redis_snapshot: dict[str, Any]) -> float | None:
        waits: list[float] = []
        for key in ("request_log_queue", "logging_event_queue"):
            queue = redis_snapshot.get(key)
            if not isinstance(queue, dict):
                continue
            for field in ("oldest_queued_wait_ms", "oldest_processing_wait_ms"):
                value = SystemMetricsService._as_float(queue.get(field))
                if value is not None:
                    waits.append(value)
        return max(waits) if waits else None

    @classmethod
    def _build_host_resource_alert_events(cls, host: dict[str, Any]) -> dict[str, dict[str, Any]]:
        events: dict[str, dict[str, Any]] = {}
        memory = host.get("memory") if isinstance(host.get("memory"), dict) else {}
        memory_percent = cls._as_float(memory.get("percent"))
        if memory_percent is not None and memory_percent >= cls.MEMORY_USAGE_WARNING_PERCENT:
            events["monitoring:host_memory_high"] = cls._event(
                "monitoring:host_memory_high",
                "system",
                "warning",
                "宿主内存使用率过高",
                f"宿主或当前运行环境内存使用率 {memory_percent}%",
                memory,
            )

        disk = host.get("disk") if isinstance(host.get("disk"), dict) else {}
        partition = disk.get("project_partition") if isinstance(disk.get("project_partition"), dict) else {}
        disk_percent = cls._as_float(partition.get("percent"))
        if disk_percent is not None and disk_percent >= cls.DISK_USAGE_WARNING_PERCENT:
            events["monitoring:project_partition_disk_high"] = cls._event(
                "monitoring:project_partition_disk_high",
                "system",
                "danger",
                "项目所在分区磁盘空间不足",
                f"项目所在分区使用率 {disk_percent}%",
                partition,
            )

        process = host.get("process") if isinstance(host.get("process"), dict) else {}
        fd_usage_ratio = cls._as_float(process.get("fd_usage_ratio"))
        if fd_usage_ratio is not None and fd_usage_ratio >= cls.FD_USAGE_WARNING_RATIO:
            events["monitoring:process_fd_high"] = cls._event(
                "monitoring:process_fd_high",
                "system",
                "warning",
                "进程文件描述符接近上限",
                f"当前进程 FD/handle 使用率 {round(fd_usage_ratio * 100, 2)}%",
                process,
            )

        network = host.get("network") if isinstance(host.get("network"), dict) else {}
        tcp_counts = network.get("tcp_status_counts") if isinstance(network.get("tcp_status_counts"), dict) else {}
        time_wait_count = int(tcp_counts.get("TIME_WAIT") or 0)
        if time_wait_count >= cls.TCP_TIME_WAIT_WARNING_THRESHOLD:
            events["monitoring:tcp_time_wait_high"] = cls._event(
                "monitoring:tcp_time_wait_high",
                "system",
                "warning",
                "TCP TIME_WAIT 堆积过高",
                f"系统 TCP TIME_WAIT 连接数 {time_wait_count}",
                {"time_wait_count": time_wait_count, "tcp_status_counts": tcp_counts},
            )

        cpu_percent = cls._as_float(host.get("cpu_percent"))
        load_per_cpu = cls._as_float(host.get("load_per_cpu"))
        cpu_times = host.get("cpu_times_percent") if isinstance(host.get("cpu_times_percent"), dict) else {}
        iowait_percent = cls._as_float(cpu_times.get("iowait"))
        if (
            iowait_percent is not None
            and iowait_percent >= cls.IO_WAIT_WARNING_PERCENT
            and load_per_cpu is not None
            and load_per_cpu >= cls.LOAD_PER_CPU_WARNING_THRESHOLD
            and (cpu_percent is None or cpu_percent < 80.0)
        ):
            events["monitoring:host_iowait_high"] = cls._event(
                "monitoring:host_iowait_high",
                "system",
                "warning",
                "系统负载高且 IO wait 偏高",
                f"load/core {load_per_cpu}，IO wait {iowait_percent}%",
                {
                    "cpu_percent": cpu_percent,
                    "load_per_cpu": load_per_cpu,
                    "cpu_times_percent": cpu_times,
                },
            )

        cgroup = host.get("cgroup") if isinstance(host.get("cgroup"), dict) else {}
        cgroup_memory = cgroup.get("memory") if isinstance(cgroup.get("memory"), dict) else {}
        cgroup_memory_percent = cls._as_float(cgroup_memory.get("usage_percent"))
        if cgroup_memory_percent is not None and cgroup_memory_percent >= cls.CGROUP_USAGE_WARNING_PERCENT:
            events["monitoring:cgroup_memory_high"] = cls._event(
                "monitoring:cgroup_memory_high",
                "system",
                "danger",
                "容器内存 cgroup 接近上限",
                f"cgroup 内存使用率 {cgroup_memory_percent}%",
                cgroup_memory,
            )
        cgroup_pids = cgroup.get("pids") if isinstance(cgroup.get("pids"), dict) else {}
        cgroup_pids_percent = cls._as_float(cgroup_pids.get("usage_percent"))
        if cgroup_pids_percent is not None and cgroup_pids_percent >= cls.CGROUP_USAGE_WARNING_PERCENT:
            events["monitoring:cgroup_pids_high"] = cls._event(
                "monitoring:cgroup_pids_high",
                "system",
                "warning",
                "容器进程数 cgroup 接近上限",
                f"cgroup pids 使用率 {cgroup_pids_percent}%",
                cgroup_pids,
            )
        return events

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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

from app.utils.timezone import now_beijing, timestamp_to_beijing