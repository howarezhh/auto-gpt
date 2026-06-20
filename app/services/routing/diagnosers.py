from __future__ import annotations

from typing import Any

from app.services.routing.decision import RouteDiagnostics, RouteStageTrace
from app.services.routing.enums import RouteCandidateAction


class StageTraceDiagnoser:
    id = "stage_trace_diagnoser"

    @staticmethod
    def diagnose(stage_traces: list[RouteStageTrace], fallback_payload: dict[str, Any] | None = None) -> RouteDiagnostics:
        reason_counts: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        for stage_trace in stage_traces:
            summary_counts = stage_trace.summary.get("reason_counts")
            has_summary_counts = isinstance(summary_counts, dict)
            if isinstance(summary_counts, dict):
                for reason, count in summary_counts.items():
                    reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + int(count or 0)
            for event in stage_trace.events:
                if event.action != RouteCandidateAction.REJECTED:
                    continue
                if not has_summary_counts:
                    reason_counts[event.reason_code] = reason_counts.get(event.reason_code, 0) + 1
                if len(samples) < 8:
                    sample = {
                        "reason": event.reason_code,
                        "reason_label": event.reason_label,
                        "provider_id": event.provider_id,
                        "provider_model_id": event.provider_model_id,
                    }
                    sample.update(event.details)
                    samples.append(sample)
        payload = dict(fallback_payload or {})
        if reason_counts:
            payload["reason_counts"] = reason_counts
        if samples and not payload.get("samples"):
            payload["samples"] = samples
        return RouteDiagnostics(payload=payload)
