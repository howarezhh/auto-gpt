from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.config import get_settings
from app.services.cache_service import CacheService
from app.services.model_catalog_service import ModelCatalogService
from app.services.provider_service import ProviderService
from app.services.router_service import RouterService
from app.services.system_metrics_service import SystemMetricsService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    settings = get_settings()
    _assert(settings.cache_l1_ttl_cap_seconds >= 5.0, "L1 cache TTL cap should favor higher hot-path hit ratio")
    _assert(settings.api_key_auth_l1_cache_ttl_seconds >= 5.0, "API key auth L1 cache should keep hot keys longer")
    _assert(settings.cache_l1_max_entries >= 1000, "L1 cache should have a practical max entry budget")

    CacheService.reset_stats()
    CacheService.set("stage26-cache-ratio:key", {"ok": True}, ttl_seconds=30)
    cached = CacheService.get("stage26-cache-ratio:key")
    stats = CacheService.stats_snapshot()
    _assert(cached == {"ok": True}, "cache should return the value just written")
    _assert(stats["memory_hits"] >= 1, f"expected a memory hit after set/get: {stats}")
    _assert(stats["hit_ratio"] == 1.0, f"cache hit ratio should be 1.0 for set/get scenario: {stats}")
    _assert(stats["memory_entries"] >= 1, f"cache should report memory entries: {stats}")
    CacheService.invalidate_prefix("stage26-cache-ratio")

    setting_snapshot = SimpleNamespace(
        route_candidate_cache_ttl_sec=12,
        model_list_cache_ttl_sec=45,
    )
    with patch("app.services.router_service.SettingService.get_cached", return_value=setting_snapshot):
        _assert(RouterService._route_candidate_cache_ttl_seconds() == 12, "route candidate cache TTL should use setting")
    with patch("app.services.provider_service.SettingService.get_cached", return_value=setting_snapshot):
        _assert(ProviderService._runtime_provider_cache_ttl_seconds() == 12, "provider runtime cache TTL should use route setting")
    with patch("app.services.setting_service.SettingService.get_cached", return_value=setting_snapshot):
        _assert(ModelCatalogService._model_list_cache_ttl_seconds() == 45, "model list cache TTL should use setting")

    metrics = {
        "status": "ok",
        "database": {"ok": True},
        "redis": {"ok": True},
        "background": {"pending_finalize_logs": 0},
        "traffic": {"error_rate_5xx": 0, "error_rate_429": 0},
    }
    with patch.object(SystemMetricsService, "_database_snapshot", return_value={"ok": True}), patch.object(
        SystemMetricsService,
        "_limits_snapshot",
        return_value={},
    ), patch.object(SystemMetricsService, "_redis_snapshot", return_value={"ok": True}), patch.object(
        SystemMetricsService,
        "_runtime_snapshot",
        return_value={},
    ), patch.object(SystemMetricsService, "_host_snapshot", return_value={}), patch.object(
        SystemMetricsService,
        "_traffic_snapshot",
        return_value=metrics["traffic"],
    ), patch.object(SystemMetricsService, "_provider_snapshot", return_value=[]), patch.object(
        SystemMetricsService,
        "_background_snapshot",
        return_value=metrics["background"],
    ), patch.object(SystemMetricsService, "_database_pool_snapshot", return_value={}), patch.object(
        SystemMetricsService,
        "_resolve_status",
        return_value="ok",
    ), patch.object(SystemMetricsService, "_evaluate_alerts", return_value=[]), patch(
        "app.services.system_metrics_service.LogService.metric_timeseries",
        return_value=[],
    ):
        collected = SystemMetricsService.collect(db=None, window_minutes=5)
    _assert("cache" in collected, "system metrics should expose cache stats")
    _assert("hit_ratio" in collected["cache"], f"cache metrics should include hit ratio: {collected['cache']}")

    print("stage26 cache ratio regression check passed")


if __name__ == "__main__":
    main()
