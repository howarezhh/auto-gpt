from __future__ import annotations

from app.services.routing.diagnosers import StageTraceDiagnoser
from app.services.routing.decision import RouteCandidateTraceEvent, RouteStageTrace
from app.services.routing.enums import RouteCandidateAction, RouteStage, RouteStageResult


def test_stage_trace_diagnoser_aggregates_rejected_events() -> None:
    trace = RouteStageTrace(
        stage=RouteStage.FILTERS,
        strategy_id="availability_first",
        result=RouteStageResult.FAILED,
        input_count=2,
        output_count=0,
        events=[
            RouteCandidateTraceEvent(
                provider_id=1,
                provider_model_id=11,
                action=RouteCandidateAction.REJECTED,
                reason_code="model_name_mismatch",
                reason_label="模型ID不匹配",
            ),
            RouteCandidateTraceEvent(
                provider_id=2,
                provider_model_id=22,
                action=RouteCandidateAction.REJECTED,
                reason_code="model_name_mismatch",
                reason_label="模型ID不匹配",
            ),
        ],
    )

    diagnostics = StageTraceDiagnoser.diagnose([trace]).to_dict()

    assert diagnostics["reason_counts"] == {"model_name_mismatch": 2}
    assert diagnostics["samples"][0]["provider_id"] == 1
    assert diagnostics["samples"][0]["reason_label"] == "模型ID不匹配"


def test_stage_trace_diagnoser_prefers_summary_counts_without_losing_samples() -> None:
    trace = RouteStageTrace(
        stage=RouteStage.FILTERS,
        strategy_id="availability_first",
        result=RouteStageResult.FAILED,
        input_count=3,
        output_count=0,
        events=[
            RouteCandidateTraceEvent(
                provider_id=1,
                provider_model_id=None,
                action=RouteCandidateAction.REJECTED,
                reason_code="provider_disabled",
                reason_label="提供商已禁用",
            )
        ],
        summary={"reason_counts": {"provider_disabled": 3}},
    )

    diagnostics = StageTraceDiagnoser.diagnose([trace]).to_dict()

    assert diagnostics["reason_counts"] == {"provider_disabled": 3}
    assert diagnostics["samples"][0]["reason"] == "provider_disabled"
