from __future__ import annotations

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.health_service import HealthService


class FakeDb:
    def commit(self) -> None:
        pass


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    provider = Provider(
        id=2701,
        name="部分可用健康检查提供商",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="upstream-secret",
        enabled=True,
        auto_circuit_break_enabled=True,
        health_status="unknown",
        circuit_state="closed",
    )
    provider_model = ProviderModel(
        id=27011,
        provider_id=2701,
        model_name="chat-only-compatible-model",
        enabled=True,
        health_status="unknown",
        circuit_state="closed",
        failure_count=5,
        success_count=0,
        supports_chat_completions=True,
        supports_responses=True,
    )
    provider.provider_models = [provider_model]

    model_result = HealthService._build_model_result(
        provider,
        provider_model,
        [
            {
                "endpoint_path": "/chat/completions",
                "endpoint_label": "chat",
                "success": True,
                "native_success": True,
                "adapted_success": False,
                "support_mode": "native",
                "support_label": "原生支持 chat/completions",
                "latency_ms": 120,
                "status_code": 200,
                "message": "ok",
                "trace": [],
            },
            {
                "endpoint_path": "/responses",
                "endpoint_label": "responses",
                "success": False,
                "native_success": False,
                "adapted_success": False,
                "support_mode": "unsupported",
                "support_label": "不支持 responses",
                "latency_ms": 80,
                "status_code": 404,
                "message": "not implemented",
                "trace": [],
            },
        ],
    )

    _assert(model_result["success"] is False, "not every selected endpoint should be marked fully successful")
    _assert(model_result["provider_success"] is True, "any successful endpoint should keep provider-level usability")
    _assert(model_result["health_status"] == "degraded", "partial endpoint success should be degraded, not unhealthy")

    HealthService._apply_model_health(
        FakeDb(),
        provider,
        provider_model,
        health_status=model_result["health_status"],
        latency_ms=model_result["latency_ms"],
        error_message=model_result["message"],
    )

    _assert(provider.health_status == "degraded", "provider aggregate availability should show partial usability")
    _assert(provider.circuit_state == "closed", "partial usability must not open provider circuit")
    _assert(provider_model.health_status == "degraded", "model should remain degraded")
    _assert(provider_model.circuit_state == "closed", "partial usability must not open model circuit")
    _assert(provider_model.failure_count == 0, "degraded health probe must not accumulate into circuit-break failure")

    print("stage27 provider partial health regression check passed")


if __name__ == "__main__":
    main()
