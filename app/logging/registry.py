from __future__ import annotations

from typing import Any

from app.models.logging_events import (
    AssetEvent,
    BackgroundJobEvent,
    BillingProcessEvent,
    ExceptionEvent,
    HealthCheckRun,
    HealthProbeEvent,
    RequestAuthEvent,
    RequestBillingEvent,
    RequestContentGuardEvent,
    RequestErrorResponseEvent,
    RequestModelPermissionEvent,
    RequestProviderAttemptEvent,
    RequestRouteDecisionEvent,
    RequestStreamEvent,
    RequestUpstreamResponseEvent,
    RequestValidationEvent,
    TokenFinalizeEvent,
    UserOperationAuditLog,
)


EVENT_MODEL_REGISTRY: dict[str, type] = {
    "request_auth": RequestAuthEvent,
    "request_validation": RequestValidationEvent,
    "request_model_permission": RequestModelPermissionEvent,
    "request_route_decision": RequestRouteDecisionEvent,
    "request_provider_attempt": RequestProviderAttemptEvent,
    "request_upstream_response": RequestUpstreamResponseEvent,
    "request_stream": RequestStreamEvent,
    "request_error_response": RequestErrorResponseEvent,
    "request_content_guard": RequestContentGuardEvent,
    "request_billing": RequestBillingEvent,
    "exception": ExceptionEvent,
    "health_check_run": HealthCheckRun,
    "health_probe": HealthProbeEvent,
    "token_finalize": TokenFinalizeEvent,
    "billing_process": BillingProcessEvent,
    "background_job": BackgroundJobEvent,
    "user_operation": UserOperationAuditLog,
    "asset": AssetEvent,
}


def model_for_event(event_name: str) -> type | None:
    return EVENT_MODEL_REGISTRY.get(event_name)


def columns_for_model(model: type) -> set[str]:
    return {column.name for column in model.__table__.columns}


def filter_payload_for_model(model: type, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = columns_for_model(model)
    return {key: value for key, value in payload.items() if key in allowed}
