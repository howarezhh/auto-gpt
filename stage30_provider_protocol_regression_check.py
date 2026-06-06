from __future__ import annotations

from unittest.mock import patch

from app.schemas.provider import normalize_provider_protocol_type
from app.services.provider_service import ProviderService
from app.services.router_service import RouterService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _FakeProviderModel:
    def __init__(
        self,
        *,
        model_id: int,
        model_name: str = "glm-5.1",
        supports_chat_completions: bool = True,
        supports_responses: bool = True,
    ) -> None:
        self.id = model_id
        self.model_name = model_name
        self.enabled = True
        self.priority = 100
        self.weight = 100
        self.health_status = "healthy"
        self.circuit_state = "closed"
        self.supports_stream = True
        self.supports_vision = False
        self.supports_tools = True
        self.supports_chat_completions = supports_chat_completions
        self.supports_responses = supports_responses


class _FakeProvider:
    def __init__(
        self,
        *,
        provider_id: int,
        name: str,
        protocol_type: str,
        supports_chat_completions: bool = True,
        supports_responses: bool = True,
    ) -> None:
        self.id = provider_id
        self.name = name
        self.protocol_type = protocol_type
        self.base_url = "https://example.com/v1"
        self.api_key = "sk-test"
        self.provider_type = "openai_compatible"
        self.group_name = None
        self.region_tag = None
        self.enabled = True
        self.priority = 100
        self.weight = 100
        self.timeout_ms = 30000
        self.max_retries = 1
        self.max_error_rate = 80.0
        self.max_active_requests = 20
        self.max_active_streams = 10
        self.max_qps = 20
        self.max_rpm = 20
        self.maintenance_mode_enabled = False
        self.health_status = "healthy"
        self.circuit_state = "closed"
        self.provider_models = [
            _FakeProviderModel(
                model_id=provider_id * 10,
                supports_chat_completions=supports_chat_completions,
                supports_responses=supports_responses,
            )
        ]


def _protocol_values_should_normalize_consistently() -> None:
    _assert(normalize_provider_protocol_type("双协议") == "both", "中文双协议应归一为 both")
    _assert(normalize_provider_protocol_type("Chat Completions API") == "chat_completions", "Chat 英文协议应归一")
    _assert(normalize_provider_protocol_type("Responses API") == "responses", "Responses 英文协议应归一")
    try:
        normalize_provider_protocol_type("embeddings")
    except ValueError:
        return
    raise AssertionError("无效协议值必须被拒绝")


def _provider_dict_should_expose_protocol_label() -> None:
    provider = _FakeProvider(provider_id=1, name="仅 Chat 中转", protocol_type="chat_completions")
    payload = ProviderService.provider_to_option_dict(provider)
    _assert(payload["protocol_type"] == "chat_completions", f"协议类型未输出: {payload}")
    _assert(payload["protocol_label"] == "Chat Completions API", f"协议展示值未输出: {payload}")


def _router_should_filter_provider_by_request_protocol() -> None:
    chat_provider = _FakeProvider(provider_id=1, name="仅 Chat 中转", protocol_type="chat_completions")
    responses_provider = _FakeProvider(provider_id=2, name="仅 Responses 中转", protocol_type="responses", supports_responses=False)
    both_provider = _FakeProvider(provider_id=3, name="双协议中转", protocol_type="both", supports_responses=False)
    providers = [chat_provider, responses_provider, both_provider]

    with (
        patch("app.services.router_service.ProviderService.list_runtime_providers", return_value=providers),
        patch("app.services.router_service.ProviderService.list_providers", return_value=providers),
        patch("app.services.router_service.ModelCatalogService.enabled_model_name_set", return_value={"glm-5.1"}),
        patch("app.services.router_service.LogService.route_metric_summary", return_value={}),
        patch("app.services.router_service.ProviderCapacityService.snapshots", return_value={}),
        patch("app.services.router_service.ProviderHealthStateService.get_model_state", return_value={}),
        patch("app.services.router_service.ProviderHealthStateService.get_provider_state", return_value={}),
        patch("app.services.router_service.ProviderHealthStateService.get_model_capability_state", return_value={}),
        patch("app.services.router_service.CacheService.get", return_value=None),
        patch("app.services.router_service.CacheService.set", side_effect=lambda _key, value, ttl_seconds=None: value),
        patch.object(RouterService, "_health_tier", return_value=1),
        patch.object(RouterService, "_route_score", return_value=1.0),
    ):
        chat_candidates = RouterService._load_available_candidates_uncached(
            None,
            cache_key="chat",
            model_name="glm-5.1",
            require_chat_completions=True,
        )
        responses_candidates = RouterService._load_available_candidates_uncached(
            None,
            cache_key="responses",
            model_name="glm-5.1",
            require_responses=True,
        )
        diagnostics = RouterService.diagnose_candidate_unavailability(
            None,
            model_name="glm-5.1",
            require_responses=True,
        )

    chat_provider_ids = {item.provider.id for item in chat_candidates}
    responses_provider_ids = {item.provider.id for item in responses_candidates}
    _assert(chat_provider_ids == {1, 3}, f"Chat 请求不应命中 Responses-only 中转站: {chat_provider_ids}")
    _assert(responses_provider_ids == {2, 3}, f"Responses 请求不应命中 Chat-only 中转站: {responses_provider_ids}")
    _assert(
        diagnostics["reason_counts"].get("provider_responses_protocol_not_supported") == 1,
        f"诊断应标记中转站协议不支持: {diagnostics}",
    )


def main() -> None:
    _protocol_values_should_normalize_consistently()
    _provider_dict_should_expose_protocol_label()
    _router_should_filter_provider_by_request_protocol()
    print("stage30 provider protocol regression check passed")


if __name__ == "__main__":
    main()
