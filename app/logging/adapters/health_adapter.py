from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging.dispatcher import LoggingDispatcher
from app.logging.sanitizers import dumps_sanitized
from app.models.logging_events import HealthCheckRun


class HealthLogRecorder:
    @staticmethod
    def _flatten_probe_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        nested_model_keys: set[tuple[str, str]] = set()
        for item in results:
            model_results = item.get("model_results")
            if not isinstance(model_results, list):
                continue
            for model_result in model_results:
                key = HealthLogRecorder._probe_result_identity(model_result)
                if key is not None:
                    nested_model_keys.add(key)
        for item in results:
            model_results = item.get("model_results")
            if isinstance(model_results, list):
                for model_result in model_results:
                    flattened.extend(HealthLogRecorder._flatten_single_probe_result(model_result))
                continue
            item_key = HealthLogRecorder._probe_result_identity(item)
            if item.get("scope") == "model" and item_key in nested_model_keys:
                continue
            flattened.extend(HealthLogRecorder._flatten_single_probe_result(item))
        return flattened

    @staticmethod
    def _probe_result_identity(item: dict[str, Any]) -> tuple[str, str] | None:
        provider_model_id = item.get("provider_model_id")
        if provider_model_id is not None:
            return ("provider_model_id", str(provider_model_id))
        model_name = item.get("model_name")
        if model_name:
            return ("model_name", str(model_name))
        return None

    @staticmethod
    def _flatten_single_probe_result(item: dict[str, Any]) -> list[dict[str, Any]]:
        endpoint_results = list(item.get("endpoint_results") or [])
        endpoint_results.extend(list(item.get("content_probe_results") or []))
        return endpoint_results or [item]

    @staticmethod
    def start_run(db: Session, *, trigger_type: str, scope_type: str, scope_id: str | int | None = None, phase_keys: list[str] | set[str] | None = None, auto_commit: bool = True) -> HealthCheckRun:
        run_id = uuid4().hex
        event = LoggingDispatcher.build_event(
            event_type="health_check",
            event_name="health_check_run",
            correlation_id=run_id,
            module="health",
            result="success",
            payload={
                "run_id": run_id,
                "trigger_type": trigger_type,
                "scope_type": scope_type,
                "scope_id": str(scope_id) if scope_id is not None else None,
                "phase_keys_json": dumps_sanitized(sorted(phase_keys or [])),
                "started_at": now_beijing(),
                "overall_result": "running",
            },
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)

    @staticmethod
    def finish_run(db: Session, *, run_id: str, results: list[dict[str, Any]], auto_commit: bool = True) -> None:
        run = db.scalar(select(HealthCheckRun).where(HealthCheckRun.run_id == run_id))
        if run is None:
            return
        finished_at = now_beijing()
        probe_results = HealthLogRecorder._flatten_probe_results(results)
        success_count = sum(1 for item in probe_results if bool(item.get("success")))
        total = len(probe_results)
        run.finished_at = finished_at
        run.duration_ms = int((finished_at - run.started_at).total_seconds() * 1000)
        run.total_probes = total
        run.success_probes = success_count
        run.failed_probes = max(0, total - success_count)
        if total <= 0:
            run.overall_result = "skipped"
        elif success_count == total:
            run.overall_result = "healthy"
        elif success_count > 0:
            run.overall_result = "degraded"
        else:
            run.overall_result = "unhealthy"
        if auto_commit:
            db.commit()

    @staticmethod
    def record_probe(db: Session, *, run_id: str | None, provider_id: int | None = None, provider_model_id: int | None = None, model_name: str | None = None, probe_type: str, endpoint_path: str | None = None, protocol_type: str | None = None, success: bool, status_code: int | None = None, latency_ms: int | None = None, error_code: str | None = None, capability_result: Any = None, content_guard_result: Any = None, auto_commit: bool = True):
        event = LoggingDispatcher.build_event(
            event_type="health_check",
            event_name="health_probe",
            correlation_id=run_id,
            module="health",
            result="success" if success else "failed",
            payload={
                "run_id": run_id,
                "provider_id": provider_id,
                "provider_model_id": provider_model_id,
                "model_name": model_name,
                "probe_type": probe_type,
                "endpoint_path": endpoint_path,
                "protocol_type": protocol_type,
                "success": success,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "error_code": error_code,
                "capability_result_json": dumps_sanitized(capability_result),
                "content_guard_result_json": dumps_sanitized(content_guard_result),
            },
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)

from app.utils.timezone import now_beijing