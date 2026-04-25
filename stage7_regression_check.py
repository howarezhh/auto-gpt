from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage7-regression.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage7-regression.db"

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models.api_client_key import ApiClientKey
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.services.log_service import LogService
from app.services.proxy_service import ProxyService
from app.services.router_service import RoutePolicyContext, RouterService


class _FakeStreamResponse:
    def __init__(self, provider_name: str, model_name: str):
        self.headers = {"x-request-id": f"stream-{provider_name}"}
        self.status_code = 200
        self._chunks = [
            (
                f'data: {{"id":"stream-{provider_name}","model":"{model_name}","choices":[{{"delta":{{"content":"hello "}}}}]}}\n\n'
            ).encode("utf-8"),
            (
                'data: {"choices":[{"delta":{"content":"world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":8,"completion_tokens":12,"total_tokens":20}}\n\n'
            ).encode("utf-8"),
            b"data: [DONE]\n\n",
        ]

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


@asynccontextmanager
async def _fake_stream_request(provider, endpoint_path: str, payload: dict):
    yield _FakeStreamResponse(provider.name, payload.get("model", "unknown"))


async def _fake_forward_json(provider, endpoint_path: str, payload: dict):
    model_name = payload.get("model", "unknown")
    if endpoint_path == "/responses":
        return {
            "id": f"resp-{provider.name}",
            "model": model_name,
            "created": 1_717_171_717,
            "output_text": f"response from {provider.name}",
            "usage": {
                "input_tokens": 30,
                "output_tokens": 20,
                "total_tokens": 50,
            },
            "output": [
                {
                    "content": [{"text": f"response from {provider.name}"}],
                    "status": "completed",
                }
            ],
        }, f"upstream-{provider.name}"
    return {
        "id": f"chat-{provider.name}",
        "model": model_name,
        "created": 1_717_171_717,
        "choices": [
            {
                "message": {"content": f"chat from {provider.name}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 30,
            "completion_tokens": 20,
            "total_tokens": 50,
        },
    }, f"upstream-{provider.name}"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _bootstrap_admin(client: TestClient) -> None:
    response = client.post(
        "/setup-admin",
        data={
            "username": "stage7-admin",
            "email": "stage7-admin@example.com",
            "password": "Stage7Admin#123",
            "password_confirm": "Stage7Admin#123",
        },
        follow_redirects=False,
    )
    _assert(response.status_code == 303, f"bootstrap admin failed: {response.text}")


def _create_provider(client: TestClient, *, name: str, priority: int, weight: int, models: list[str]) -> dict:
    payload = {
        "name": name,
        "base_url": "https://example.com/v1",
        "api_key": "upstream-secret",
        "provider_type": "openai_compatible",
        "enabled": True,
        "priority": priority,
        "weight": weight,
        "timeout_ms": 30000,
        "max_retries": 1,
        "model_configs": [
            {
                "model_name": model_name,
                "priority": 100,
                "weight": weight,
                "supports_stream": True,
                "supports_vision": "vision" in model_name,
                "enabled": True,
                "input_price_per_1k": 0.0005 if model_name == "reg-model" else 0.0008,
                "output_price_per_1k": 0.0015 if model_name == "reg-model" else 0.002,
            }
            for model_name in models
        ],
        "remark": f"stage7 {name}",
    }
    response = client.post("/api/providers", json=payload)
    _assert(response.status_code == 201, f"create provider failed: {response.text}")
    return response.json()


def _create_api_key(
    client: TestClient,
    *,
    name: str,
    allowed_provider_ids: list[int],
    default_provider_id: int | None,
    enabled: bool = True,
    route_mode: str = "failover",
    manual_allow_fallback: bool = True,
    token_limit_total: int | None = 1000,
    cost_limit_total: float | None = None,
    balance_amount: float | None = None,
    expires_at: str | None = None,
) -> dict:
    payload = {
        "name": name,
        "remark": f"stage7 {name}",
        "enabled": enabled,
        "expires_at": expires_at,
        "token_limit_total": token_limit_total,
        "cost_limit_total": cost_limit_total,
        "balance_amount": balance_amount,
        "route_mode": route_mode,
        "default_provider_id": default_provider_id,
        "manual_allow_fallback": manual_allow_fallback,
        "allowed_provider_ids": allowed_provider_ids,
    }
    response = client.post("/api/api-keys", json=payload)
    _assert(response.status_code == 201, f"create api key failed: {response.text}")
    return response.json()


def main() -> None:
    with patch.object(ProxyService, "_forward_json", side_effect=_fake_forward_json), patch.object(
        ProxyService, "_stream_request", side_effect=_fake_stream_request
    ):
        with TestClient(app) as client:
            _bootstrap_admin(client)
            provider_a = _create_provider(
                client,
                name="stage7-provider-a",
                priority=10,
                weight=100,
                models=["reg-model", "alpha-only"],
            )
            provider_b = _create_provider(
                client,
                name="stage7-provider-b",
                priority=20,
                weight=300,
                models=["reg-model", "beta-only"],
            )
            inferred_provider_response = client.post(
                "/api/providers",
                json={
                    "name": "stage7-provider-inferred",
                    "base_url": "https://example.com/v1",
                    "api_key": "upstream-secret",
                    "provider_type": "openai_compatible",
                    "enabled": True,
                    "priority": 30,
                    "weight": 100,
                    "timeout_ms": 30000,
                    "max_retries": 1,
                    "models": ["gpt-5.4"],
                    "remark": "stage7 inferred",
                },
            )
            _assert(inferred_provider_response.status_code == 201, f"inferred provider create failed: {inferred_provider_response.text}")
            inferred_provider = inferred_provider_response.json()
            inferred_model = next((item for item in inferred_provider["model_configs"] if item["model_name"] == "gpt-5.4"), None)
            _assert(inferred_model is not None, "inferred provider gpt-5.4 model missing")
            _assert(inferred_model["supports_vision"] is True, "models list should infer gpt-5.4 vision support")

            active_key = _create_api_key(
                client,
                name="stage7-active",
                allowed_provider_ids=[provider_a["id"]],
                default_provider_id=provider_a["id"],
                route_mode="failover",
                manual_allow_fallback=True,
                token_limit_total=1000,
                cost_limit_total=1,
                balance_amount=1,
            )
            disabled_key = _create_api_key(
                client,
                name="stage7-disabled",
                allowed_provider_ids=[provider_a["id"]],
                default_provider_id=provider_a["id"],
                enabled=False,
            )
            expired_key = _create_api_key(
                client,
                name="stage7-expired",
                allowed_provider_ids=[provider_a["id"]],
                default_provider_id=provider_a["id"],
                expires_at="2000-01-01T00:00:00Z",
            )
            quota_key = _create_api_key(
                client,
                name="stage7-quota",
                allowed_provider_ids=[provider_a["id"]],
                default_provider_id=provider_a["id"],
                token_limit_total=10,
            )
            unbound_key = _create_api_key(
                client,
                name="stage7-unbound",
                allowed_provider_ids=[],
                default_provider_id=None,
                token_limit_total=1000,
            )
            vision_key = _create_api_key(
                client,
                name="stage7-vision",
                allowed_provider_ids=[provider_a["id"], provider_b["id"]],
                default_provider_id=provider_a["id"],
                token_limit_total=1000,
            )
            balance_key = _create_api_key(
                client,
                name="stage7-balance",
                allowed_provider_ids=[provider_a["id"]],
                default_provider_id=provider_a["id"],
                token_limit_total=1000,
                balance_amount=0.00004,
            )
            invalid_manual_key_response = client.post(
                "/api/api-keys",
                json={
                    "name": "stage7-invalid-manual",
                    "remark": "invalid manual",
                    "enabled": True,
                    "token_limit_total": 1000,
                    "route_mode": "manual",
                    "default_provider_id": None,
                    "manual_allow_fallback": False,
                    "allowed_provider_ids": [provider_a["id"]],
                },
            )
            _assert(
                invalid_manual_key_response.status_code == 400,
                f"manual api key without default should be rejected: {invalid_manual_key_response.text}",
            )
            invalid_manual_settings_response = client.put(
                "/api/settings",
                json={
                    "route_mode": "manual",
                    "default_provider_id": None,
                    "manual_allow_fallback": False,
                    "global_timeout_ms": 30000,
                    "global_max_retries": 2,
                    "circuit_breaker_threshold": 3,
                    "auto_health_check": True,
                    "health_check_interval_sec": 60,
                    "recovery_probe_interval_sec": 30,
                    "enable_token_logging": True,
                    "enable_payload_logging": True,
                    "enable_stream_response_persist": True,
                    "mask_sensitive_fields": True,
                    "max_logged_body_bytes": 16384,
                },
            )
            _assert(
                invalid_manual_settings_response.status_code == 400,
                f"manual global settings without default should be rejected: {invalid_manual_settings_response.text}",
            )

            with SessionLocal() as db:
                provider_a_record = db.get(Provider, provider_a["id"])
                provider_b_record = db.get(Provider, provider_b["id"])
                _assert(provider_a_record is not None, "provider_a missing")
                _assert(provider_b_record is not None, "provider_b missing")
                for provider_record, vision_enabled in ((provider_a_record, False), (provider_b_record, True)):
                    target_model = next((item for item in provider_record.provider_models if item.model_name == "reg-model"), None)
                    _assert(target_model is not None, f"reg-model missing on provider {provider_record.id}")
                    target_model.supports_vision = vision_enabled
                quota_record = db.get(ApiClientKey, quota_key["id"])
                _assert(quota_record is not None, "quota api key missing")
                quota_record.prompt_tokens_used = 5
                quota_record.completion_tokens_used = 5
                quota_record.total_tokens_used = 10
                db.commit()

            invalid_response = client.get("/v1/models", headers={"Authorization": "Bearer not-real"})
            _assert(invalid_response.status_code == 401, f"invalid key should be 401: {invalid_response.text}")

            disabled_response = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {disabled_key['raw_api_key']}"},
            )
            _assert(disabled_response.status_code == 403, f"disabled key should be 403: {disabled_response.text}")

            expired_response = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {expired_key['raw_api_key']}"},
            )
            _assert(expired_response.status_code == 403, f"expired key should be 403: {expired_response.text}")

            quota_response = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {quota_key['raw_api_key']}"},
            )
            _assert(quota_response.status_code == 429, f"quota key should be 429: {quota_response.text}")

            unbound_response = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {unbound_key['raw_api_key']}"},
            )
            _assert(unbound_response.status_code == 403, f"unbound key should be 403: {unbound_response.text}")

            visible_models = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {active_key['raw_api_key']}"},
            )
            _assert(visible_models.status_code == 200, f"valid /v1/models failed: {visible_models.text}")
            visible_model_ids = {item["id"] for item in visible_models.json()["data"]}
            _assert("alpha-only" in visible_model_ids, "authorized model missing from /v1/models")
            _assert("beta-only" not in visible_model_ids, "unauthorized model leaked into /v1/models")

            external_json = client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {active_key['raw_api_key']}",
                    "X-Aotu-Provider-Id": str(provider_b["id"]),
                },
                json={"model": "reg-model", "messages": [{"role": "user", "content": "hello"}]},
            )
            _assert(external_json.status_code == 200, f"external chat request failed: {external_json.text}")
            _assert(
                external_json.headers.get("X-Proxy-Provider-Id") == str(provider_a["id"]),
                "external /v1/* should ignore internal forced provider header",
            )

            stream_response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {active_key['raw_api_key']}"},
                json={"model": "reg-model", "messages": [{"role": "user", "content": "stream"}], "stream": True},
            )
            _assert(stream_response.status_code == 200, f"stream request failed: {stream_response.text}")
            _assert("data:" in stream_response.text, "stream response should return SSE payload")

            balance_spend_response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {balance_key['raw_api_key']}"},
                json={"model": "reg-model", "messages": [{"role": "user", "content": "bill me"}]},
            )
            _assert(balance_spend_response.status_code == 200, f"balance spend request failed: {balance_spend_response.text}")
            balance_exhausted_response = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {balance_key['raw_api_key']}"},
            )
            _assert(balance_exhausted_response.status_code == 429, f"balance exhausted key should be 429: {balance_exhausted_response.text}")

            providers_response = client.get("/api/providers")
            _assert(providers_response.status_code == 200, f"provider list failed: {providers_response.text}")
            providers_payload = providers_response.json()
            provider_a_after_calls = next((item for item in providers_payload if item["id"] == provider_a["id"]), None)
            _assert(provider_a_after_calls is not None, "provider_a missing from provider list")
            reg_model_metrics = next((item for item in provider_a_after_calls["model_configs"] if item["model_name"] == "reg-model"), None)
            _assert(reg_model_metrics is not None, "provider reg-model missing after requests")
            _assert(reg_model_metrics["input_price_per_1k"] == 0.0005, f"input price not persisted: {reg_model_metrics}")
            _assert(reg_model_metrics["output_price_per_1k"] == 0.0015, f"output price not persisted: {reg_model_metrics}")
            _assert(reg_model_metrics["recent_request_count"] >= 2, f"recent request count missing: {reg_model_metrics}")
            _assert(reg_model_metrics["success_rate"] is not None, f"success rate missing: {reg_model_metrics}")
            _assert(reg_model_metrics["stability_score"] is not None, f"stability score missing: {reg_model_metrics}")
            _assert(reg_model_metrics["avg_first_token_latency_ms"] is not None, f"first token metric missing: {reg_model_metrics}")
            _assert(provider_a_after_calls["best_input_price_per_1k"] == 0.0005, f"provider best input price mismatch: {provider_a_after_calls}")
            _assert(provider_a_after_calls["success_rate"] is not None, f"provider success rate missing: {provider_a_after_calls}")

            internal_playground = client.post(
                "/api/playground/responses",
                headers={"X-Aotu-Provider-Id": str(provider_b["id"])},
                json={"model": "reg-model", "input": "test internal response"},
            )
            _assert(internal_playground.status_code == 200, f"internal playground failed: {internal_playground.text}")
            _assert(
                internal_playground.headers.get("X-Proxy-Provider-Id") == str(provider_b["id"]),
                "internal playground should still support specified provider testing",
            )

            vision_response = client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {vision_key['raw_api_key']}"},
                json={
                    "model": "reg-model",
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "describe image"},
                                {"type": "input_image", "image_url": "https://example.com/demo.png"},
                            ],
                        }
                    ],
                },
            )
            _assert(vision_response.status_code == 200, f"vision responses request failed: {vision_response.text}")
            _assert(
                vision_response.headers.get("X-Proxy-Provider-Id") == str(provider_b["id"]),
                "image request should route to a vision-capable provider",
            )

            with SessionLocal() as db:
                stream_logs = list(
                    db.query(RequestLog)
                    .filter(RequestLog.is_stream.is_(True), RequestLog.success.is_(True))
                    .all()
                )
                _assert(stream_logs, "expected successful stream logs")
                _assert(
                    any(log.first_token_latency_ms is not None for log in stream_logs),
                    "stream log should persist first_token_latency_ms",
                )

                manual_order = RouterService.order_candidates(
                    db,
                    model_name="reg-model",
                    route_context=RoutePolicyContext(
                        route_mode="manual",
                        default_provider_id=provider_a["id"],
                        manual_allow_fallback=False,
                        allowed_provider_ids=[provider_a["id"], provider_b["id"]],
                    ),
                )
                _assert(
                    [item.provider.id for item in manual_order] == [provider_a["id"]],
                    "manual mode without fallback should only keep default provider",
                )

                failover_order = RouterService.order_candidates(
                    db,
                    model_name="reg-model",
                    route_context=RoutePolicyContext(
                        route_mode="failover",
                        default_provider_id=provider_a["id"],
                        manual_allow_fallback=True,
                        allowed_provider_ids=[provider_a["id"], provider_b["id"]],
                    ),
                )
                _assert(
                    failover_order[0].provider.id == provider_a["id"] and {item.provider.id for item in failover_order} == {provider_a["id"], provider_b["id"]},
                    "failover mode ordering is incorrect",
                )

                with patch("app.services.router_service.random.choices", side_effect=lambda population, weights, k: [population[-1]]):
                    weighted_order = RouterService.order_candidates(
                        db,
                        model_name="reg-model",
                        route_context=RoutePolicyContext(
                            route_mode="weighted",
                            default_provider_id=provider_a["id"],
                            manual_allow_fallback=True,
                            allowed_provider_ids=[provider_a["id"], provider_b["id"]],
                        ),
                    )
                _assert(weighted_order[0].provider.id == provider_b["id"], "weighted mode did not honor weighted first pick")

                sticky_first = RouterService.order_candidates(
                    db,
                    model_name="reg-model",
                    sticky_key="sticky-user",
                    route_context=RoutePolicyContext(
                        route_mode="sticky",
                        default_provider_id=provider_a["id"],
                        manual_allow_fallback=True,
                        allowed_provider_ids=[provider_a["id"], provider_b["id"]],
                    ),
                )
                sticky_second = RouterService.order_candidates(
                    db,
                    model_name="reg-model",
                    sticky_key="sticky-user",
                    route_context=RoutePolicyContext(
                        route_mode="sticky",
                        default_provider_id=provider_a["id"],
                        manual_allow_fallback=True,
                        allowed_provider_ids=[provider_a["id"], provider_b["id"]],
                    ),
                )
                _assert(
                    sticky_first[0].provider.id == sticky_second[0].provider.id,
                    "sticky mode should keep the same first candidate for same sticky key",
                )

                allowed_only = RouterService.order_candidates(
                    db,
                    model_name="reg-model",
                    route_context=RoutePolicyContext(
                        route_mode="failover",
                        default_provider_id=provider_a["id"],
                        manual_allow_fallback=True,
                        allowed_provider_ids=[provider_a["id"]],
                    ),
                )
                _assert(
                    {item.provider.id for item in allowed_only} == {provider_a["id"]},
                    "allowed_provider_ids filter did not take effect",
                )

                for _ in range(5):
                    db.add(
                        RequestLog(
                            log_type="health_check_model",
                            provider_id=provider_a["id"],
                            provider_name=provider_a["name"],
                            model_name="reg-model",
                            requested_model="reg-model",
                            request_path="/chat/completions",
                            success=False,
                            latency_ms=999,
                            message="probe failed",
                        )
                    )
                provider_b_current = db.get(Provider, provider_b["id"])
                _assert(provider_b_current is not None, "current provider_b missing")
                provider_b_model = next(
                    item for item in provider_b_current.provider_models if item.model_name == "reg-model"
                )
                provider_b_current.circuit_state = "closed"
                provider_b_model.health_status = "unhealthy"
                provider_b_model.circuit_state = "open"
                provider_b_model.circuit_opened_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=120)
                db.commit()

                route_metrics = LogService.route_metric_summary(db, window_minutes=5, requested_model="reg-model")
                _assert(
                    route_metrics[(provider_a["id"], "reg-model")]["failed_requests"] == 0,
                    f"route metrics should ignore health checks: {route_metrics}",
                )

                half_open_order = RouterService.order_candidates(
                    db,
                    model_name="reg-model",
                    route_context=RoutePolicyContext(
                        route_mode="failover",
                        default_provider_id=provider_b["id"],
                        manual_allow_fallback=True,
                        allowed_provider_ids=[provider_b["id"]],
                    ),
                )
                _assert(
                    half_open_order[0].provider_model.circuit_state == "half_open",
                    "recovery probe should switch candidate to half_open",
                )

            with SessionLocal() as db:
                persisted_provider_b = db.get(Provider, provider_b["id"])
                _assert(persisted_provider_b is not None, "persisted provider_b missing")
                persisted_provider_b_model = next(
                    item for item in persisted_provider_b.provider_models if item.model_name == "reg-model"
                )
                _assert(
                    persisted_provider_b_model.circuit_state == "half_open",
                    "half_open recovery state should persist to database",
                )

            detail_response = client.get(f"/api/api-keys/{active_key['id']}")
            stats_response = client.get(f"/api/api-keys/{active_key['id']}/stats")
            analytics_response = client.get(f"/api/api-keys/{active_key['id']}/analytics")
            billing_response = client.get(f"/api/api-keys/{active_key['id']}/billing?limit=20")
            logs_response = client.get(f"/api/api-keys/{active_key['id']}/logs?page=1&page_size=50")
            summary_response = client.get("/api/api-keys/summary")
            vision_logs_response = client.get(f"/api/api-keys/{vision_key['id']}/logs?page=1&page_size=20")
            filtered_logs_response = client.get(
                f"/api/logs?api_client_key_id={active_key['id']}&exclude_health_checks=true&page=1&page_size=50"
            )
            filter_options_response = client.get("/api/logs/filter-options?exclude_health_checks=true")

            _assert(detail_response.status_code == 200, f"detail response failed: {detail_response.text}")
            _assert(stats_response.status_code == 200, f"stats response failed: {stats_response.text}")
            _assert(analytics_response.status_code == 200, f"analytics response failed: {analytics_response.text}")
            _assert(billing_response.status_code == 200, f"billing response failed: {billing_response.text}")
            _assert(logs_response.status_code == 200, f"logs response failed: {logs_response.text}")
            _assert(summary_response.status_code == 200, f"summary response failed: {summary_response.text}")
            _assert(vision_logs_response.status_code == 200, f"vision logs response failed: {vision_logs_response.text}")
            _assert(filtered_logs_response.status_code == 200, f"filtered logs response failed: {filtered_logs_response.text}")
            _assert(filter_options_response.status_code == 200, f"filter options response failed: {filter_options_response.text}")

            detail = detail_response.json()
            stats = stats_response.json()
            analytics = analytics_response.json()
            billing = billing_response.json()
            logs = logs_response.json()
            summary = summary_response.json()
            vision_logs = vision_logs_response.json()
            filtered_logs = filtered_logs_response.json()
            filter_options = filter_options_response.json()

            _assert(detail["total_tokens_used"] == 70, f"detail total tokens mismatch: {detail}")
            _assert(detail["remaining_tokens"] == 930, f"detail remaining tokens mismatch: {detail}")
            _assert(detail["total_cost_used"] > 0, f"detail total cost should be positive: {detail}")
            _assert(detail["balance_amount"] < 1, f"detail balance should be deducted: {detail}")
            _assert(stats["total_requests"] == 2, f"stats total requests mismatch: {stats}")
            _assert(stats["success_requests"] == 2, f"stats success requests mismatch: {stats}")
            _assert(stats["recent_total_tokens"] == 70, f"recent tokens mismatch: {stats}")
            _assert(stats["recent_total_cost"] > 0, f"recent total cost mismatch: {stats}")
            _assert(billing["total_billing_records"] >= 2, f"billing records should include init top-up and request charges: {billing}")
            _assert(any(item["record_type"] == "request_charge" for item in billing["items"]), f"billing items missing request charge: {billing}")
            _assert(billing["recent_billed_cost"] > 0, f"billing recent cost mismatch: {billing}")

            success_logs = [item for item in logs["items"] if item["success"]]
            _assert(len(success_logs) == 2, f"expected 2 success logs, got {len(success_logs)}")
            _assert(sum(int(item.get("total_tokens") or 0) for item in success_logs) == 70, "log token sum mismatch")
            _assert(filtered_logs["total"] == 2, f"filtered logs total mismatch: {filtered_logs}")
            _assert(filtered_logs["summary"]["total_requests"] == 2, f"filtered log summary request mismatch: {filtered_logs}")
            _assert(filtered_logs["summary"]["total_tokens"] == 70, f"filtered log summary token mismatch: {filtered_logs}")
            _assert(filtered_logs["summary"]["matched_api_keys"] == 1, f"filtered log summary key count mismatch: {filtered_logs}")
            _assert(any(item["value"] == str(provider_a["id"]) for item in filter_options["providers"]), "provider filter option missing")
            _assert(any(item["value"] == "reg-model" for item in filter_options["model_names"]), "model filter option missing")
            _assert(any(item["value"] == str(active_key["id"]) for item in filter_options["api_client_key_ids"]), "api key id filter option missing")
            _assert(any(item["value"] == active_key["key_prefix"] for item in filter_options["api_client_key_queries"]), "api key query filter option missing")
            _assert(any(item.get("has_image") for item in vision_logs["items"]), "vision request log should record has_image=true")
            _assert(analytics["model_distribution"][0]["total_tokens"] == 70, "analytics token aggregation mismatch")
            _assert(analytics["model_distribution"][0]["total_cost"] > 0, "analytics cost aggregation mismatch")
            _assert(summary["total_keys"] == 7, f"summary key count mismatch: {summary}")
            _assert(summary["total_tokens"] >= 120, f"summary total tokens should include image request usage: {summary}")
            _assert(summary["total_cost_used"] > 0, f"summary total cost mismatch: {summary}")
            _assert(summary["total_balance_amount"] >= 0, f"summary total balance mismatch: {summary}")

            balance_adjust_response = client.post(
                f"/api/api-keys/{active_key['id']}/billing/adjust",
                json={"amount": 2, "remark": "stage7 recharge"},
            )
            _assert(balance_adjust_response.status_code == 200, f"balance adjust failed: {balance_adjust_response.text}")
            adjusted_billing = balance_adjust_response.json()
            _assert(any(item["record_type"] == "top_up" for item in adjusted_billing["items"]), f"billing top-up record missing: {adjusted_billing}")

    print("stage7 regression passed")


if __name__ == "__main__":
    main()
