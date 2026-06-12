from __future__ import annotations

import time

from app.services.runtime_state_service import RuntimeStateService
from app.services.system_metrics_service import SystemMetricsService
from app.utils.json_utils import dumps_json


def test_host_resource_alerts_cover_kernel_network_disk_and_cgroup_signals() -> None:
    host = {
        "cpu_percent": 35.0,
        "load_per_cpu": 2.2,
        "cpu_times_percent": {"iowait": 31.5},
        "memory": {"percent": 94.0},
        "disk": {"project_partition": {"percent": 96.0, "path": "/srv/aotu-gpt"}},
        "process": {"fd_usage_ratio": 0.92, "fd_or_handle_count": 920, "fd_limit_soft": 1000},
        "network": {"tcp_status_counts": {"ESTABLISHED": 120, "TIME_WAIT": 6000}},
        "cgroup": {
            "memory": {"usage_percent": 91.0},
            "pids": {"usage_percent": 93.0},
        },
    }

    events = SystemMetricsService._build_host_resource_alert_events(host)

    assert "monitoring:host_memory_high" in events
    assert "monitoring:project_partition_disk_high" in events
    assert "monitoring:process_fd_high" in events
    assert "monitoring:tcp_time_wait_high" in events
    assert "monitoring:host_iowait_high" in events
    assert "monitoring:cgroup_memory_high" in events
    assert "monitoring:cgroup_pids_high" in events
    assert all(item["alert_type"] == "system" for item in events.values())


def test_empty_host_resource_snapshots_keep_stable_shapes() -> None:
    disk = SystemMetricsService._empty_disk_snapshot("/tmp/aotu-gpt")
    network = SystemMetricsService._empty_network_snapshot()
    traffic = SystemMetricsService._empty_traffic()

    assert disk["scope"] == "partition_metrics_only"
    assert disk["project_partition"]["path"] == "/tmp/aotu-gpt"
    assert disk["project_partition"]["percent"] is None
    assert "io" in disk
    assert network["tcp_status_counts"] == {}
    assert network["tcp_connection_count"] is None
    assert traffic["latency_by_mode"]["stream"]["p95_latency_ms"] is None
    assert traffic["terminal_breakdown"]["client_cancelled"] == 0


def test_terminal_breakdown_classifies_capacity_upstream_and_client_cancel() -> None:
    rows = [
        (499, "client_cancelled"),
        (429, "concurrency_limit_exceeded"),
        (503, "upstream_timeout"),
        (400, "invalid_json_body"),
    ]

    breakdown = SystemMetricsService._terminal_breakdown(rows)

    assert breakdown == {
        "client_cancelled": 1,
        "upstream_failed": 1,
        "system_limited": 1,
        "other_failed": 1,
    }


def test_queue_oldest_wait_ms_reads_request_and_logging_queue_envelopes() -> None:
    class FakeRedis:
        def __init__(self, raw_item: str) -> None:
            self.raw_item = raw_item

        def lindex(self, queue_key: str, index: int) -> str:
            return self.raw_item

    request_raw = dumps_json({"kwargs": {"_queue_enqueued_at": time.time() - 3}})
    logging_raw = dumps_json({"_queue_enqueued_at": time.time() - 2, "payload": {}, "envelope": {}})

    assert SystemMetricsService._queue_oldest_wait_ms(FakeRedis(request_raw), "queue") >= 2500
    assert SystemMetricsService._queue_oldest_wait_ms(FakeRedis(logging_raw), "queue") >= 1500


def test_monitoring_alerts_cover_event_loop_queue_wait_and_client_cancellation() -> None:
    metrics = {
        "window_minutes": 5,
        "redis": {
            "ok": True,
            "active_requests": 1,
            "active_streams": 0,
            "max_active_requests": 20,
            "max_active_streams": 10,
            "request_log_queue": {"oldest_queued_wait_ms": 2500, "oldest_processing_wait_ms": None},
            "logging_event_queue": {"oldest_queued_wait_ms": None, "oldest_processing_wait_ms": None},
        },
        "database": {"ok": True},
        "traffic": {
            "total_requests": 20,
            "status_5xx_rate": 0.0,
            "status_429": 0,
            "status_429_rate": 0.0,
            "terminal_breakdown": {
                "client_cancelled": 10,
                "upstream_failed": 0,
                "system_limited": 0,
                "other_failed": 0,
            },
        },
        "content_guard": {"enabled": False},
        "background": {"pending_finalize_logs": 0, "billing_failed_logs": 0, "token_failed_logs": 0},
        "runtime": {"event_loop": {"latest_delay_ms": 250.0, "max_delay_ms": 300.0}},
        "host": {},
        "providers": [],
    }

    events = SystemMetricsService._build_monitoring_alert_events(metrics)

    assert "monitoring:client_cancelled_spike" in events
    assert "monitoring:queue_wait_high" in events
    assert "monitoring:event_loop_delay_high" in events


def test_event_loop_delay_alert_uses_recent_window_not_lifetime_peak() -> None:
    now = time.monotonic()
    with RuntimeStateService._lock:
        RuntimeStateService._event_loop_latest_delay_ms = None
        RuntimeStateService._event_loop_max_delay_ms = None
        RuntimeStateService._event_loop_lifetime_max_delay_ms = None
        RuntimeStateService._event_loop_avg_delay_ms = None
        RuntimeStateService._event_loop_sample_count = 0
        RuntimeStateService._event_loop_recent_samples.clear()

    RuntimeStateService._record_event_loop_delay(1500.0, sampled_at=now - RuntimeStateService._event_loop_window_seconds - 5)
    RuntimeStateService._record_event_loop_delay(25.0, sampled_at=now)
    snapshot = RuntimeStateService.event_loop_snapshot()

    assert snapshot["max_delay_ms"] == 25.0
    assert snapshot["recent_max_delay_ms"] == 25.0
    assert snapshot["lifetime_max_delay_ms"] == 1500.0

    metrics = {
        "window_minutes": 5,
        "redis": {"ok": True, "active_requests": 0, "active_streams": 0},
        "database": {"ok": True},
        "traffic": {"total_requests": 0, "status_5xx_rate": 0.0, "status_429": 0, "status_429_rate": 0.0},
        "content_guard": {"enabled": False},
        "background": {"pending_finalize_logs": 0, "billing_failed_logs": 0, "token_failed_logs": 0},
        "runtime": {"event_loop": snapshot},
        "host": {},
        "providers": [],
    }

    assert "monitoring:event_loop_delay_high" not in SystemMetricsService._build_monitoring_alert_events(metrics)


def test_tcp_status_counts_from_proc_maps_time_wait(monkeypatch) -> None:
    sample = "\n".join(
        [
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode",
            "   0: 0100007F:1F90 0100007F:1770 06 00000000:00000000 00:00000000 00000000 1000 0 1",
            "   1: 0100007F:1F91 0100007F:1771 01 00000000:00000000 00:00000000 00000000 1000 0 2",
        ]
    )

    def fake_read_text(path):
        return sample if getattr(path, "name", "") == "tcp" else None

    monkeypatch.setattr(SystemMetricsService, "_read_text_file", staticmethod(fake_read_text))

    counts = SystemMetricsService._tcp_status_counts_from_proc()

    assert counts["TIME_WAIT"] == 1
    assert counts["ESTABLISHED"] == 1
