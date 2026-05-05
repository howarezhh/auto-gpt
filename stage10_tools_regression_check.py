from __future__ import annotations
import os
from pathlib import Path
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage10-tools-regression.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage10-tools-regression.db"
os.environ["ENABLE_SCHEDULER"] = "false"

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models.provider import Provider
from app.services.health_service import HealthService
from app.services.proxy_service import ProxyService
from app.services.provider_service import ProviderService
from app.services.router_service import RoutePolicyContext, RouterService
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


async def _fake_forward_json_with_endpoint_fallback(provider, provider_model, endpoint_path: str, payload: dict, *, started, setting):
    model_name = payload.get("model", "unknown")
    if payload.get("tools"):
        return (_tool_call_response(provider.name, model_name), f"upstream-{provider.name}", [])
    return (
        {
            "id": f"chatcmpl-{provider.name}",
            "model": model_name,
            "choices": [
                {
                    "message": {"role": "assistant", "content": f"hello from {provider.name}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
            },
        },
        f"upstream-{provider.name}",
        [],
    )


async def _fake_send_prepared_json(provider, *, prepared, headers, requested_payload, setting):
    model_name = prepared.request_payload.get("model", "unknown")
    if prepared.request_payload.get("tools"):
        return _tool_call_response(provider.name, model_name), f"upstream-{provider.name}"
    return {
        "id": f"chatcmpl-{provider.name}",
        "model": model_name,
        "choices": [
            {
                "message": {"role": "assistant", "content": f"hello from {provider.name}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 8,
            "completion_tokens": 4,
            "total_tokens": 12,
        },
    }, f"upstream-{provider.name}"


def _tool_call_response(provider_name: str, model_name: str) -> dict:
    return {
        "id": f"chatcmpl-{provider_name}",
        "model": model_name,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_stage10",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 18,
            "completion_tokens": 6,
            "total_tokens": 24,
        },
    }


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _login(client: TestClient, *, identifier: str, password: str) -> None:
    response = client.post(
        "/login",
        data={"identifier": identifier, "password": password},
        follow_redirects=False,
    )
    _assert(response.status_code == 303, f"login failed: {response.text}")


def _bootstrap_admin(client: TestClient) -> None:
    with SessionLocal() as db:
        if UserAuthService.get_user_by_login(db, "stage10-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage10-admin",
                email="stage10-admin@example.com",
                password="Stage10Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage10-admin", password="Stage10Admin#123")


def _create_provider_with_inferred_model(client: TestClient) -> dict:
    response = client.post(
        "/api/providers",
        json={
            "name": "stage10-glm-provider",
            "base_url": "https://example.com/v1",
            "api_key": "upstream-secret",
            "provider_type": "openai_compatible",
            "enabled": True,
            "priority": 10,
            "weight": 100,
            "timeout_ms": 30000,
            "max_retries": 1,
            "models": ["glm-5.1"],
            "remark": "stage10 inferred glm provider",
        },
    )
    _assert(response.status_code == 201, f"create inferred provider failed: {response.text}")
    return response.json()


def _create_provider_without_tools(client: TestClient) -> dict:
    response = client.post(
        "/api/providers",
        json={
            "name": "stage10-no-tools-provider",
            "base_url": "https://example.com/v1",
            "api_key": "upstream-secret",
            "provider_type": "openai_compatible",
            "enabled": True,
            "priority": 20,
            "weight": 100,
            "timeout_ms": 30000,
            "max_retries": 1,
            "model_configs": [
                {
                    "model_name": "plain-chat-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": True,
                    "enabled": True,
                    "input_price_per_1k": 0.001,
                    "output_price_per_1k": 0.002,
                }
            ],
            "remark": "stage10 no-tools provider",
        },
    )
    _assert(response.status_code == 201, f"create no-tools provider failed: {response.text}")
    return response.json()


def _create_api_key(client: TestClient, *, default_provider_id: int, allowed_provider_ids: list[int]) -> dict:
    response = client.post(
        "/api/api-keys",
        json={
            "name": "stage10-key",
            "remark": "stage10 tools regression key",
            "enabled": True,
            "token_limit_total": 5000,
            "route_mode": "failover",
            "default_provider_id": default_provider_id,
            "manual_allow_fallback": True,
            "allowed_provider_ids": allowed_provider_ids,
        },
    )
    _assert(response.status_code == 201, f"create api key failed: {response.text}")
    return response.json()


def main() -> None:
    with patch.object(ProxyService, "_forward_json_with_endpoint_fallback", side_effect=_fake_forward_json_with_endpoint_fallback), patch.object(
        ProxyService, "_send_prepared_json", side_effect=_fake_send_prepared_json
    ):
        with TestClient(app) as client:
            _bootstrap_admin(client)
            glm_provider = _create_provider_with_inferred_model(client)
            no_tools_provider = _create_provider_without_tools(client)
            api_key = _create_api_key(
                client,
                default_provider_id=no_tools_provider["id"],
                allowed_provider_ids=[no_tools_provider["id"], glm_provider["id"]],
            )

            glm_model = next((item for item in glm_provider["model_configs"] if item["model_name"] == "glm-5.1"), None)
            _assert(glm_model is not None, f"glm model config missing: {glm_provider}")
            _assert(glm_model["supports_tools"] is True, f"glm inferred supports_tools should be true: {glm_model}")
            inferred = ProviderService._infer_model_capabilities("glm-5.1")
            _assert(inferred["supports_tools"] is True, f"glm tool inference failed: {inferred}")
            _assert(
                ProxyService._payload_uses_tools(
                    {
                        "model": "glm-5.1",
                        "messages": [{"role": "user", "content": "hello"}],
                        "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
                    }
                ),
                "payload with tools should require tool-capable routing",
            )
            _assert(
                HealthService._response_has_tool_call(
                    {
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "tool_calls": [
                                        {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
                                    ]
                                },
                            }
                        ]
                    }
                ),
                "chat response tool-call detection failed",
            )
            _assert(
                HealthService._response_has_tool_call(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "name": "get_weather",
                                "arguments": "{}",
                            }
                        ]
                    }
                ),
                "responses output tool-call detection failed",
            )

            with SessionLocal() as db:
                candidates = RouterService.order_candidates(
                    db,
                    model_name="glm-5.1",
                    route_context=RoutePolicyContext(
                        route_mode="failover",
                        default_provider_id=no_tools_provider["id"],
                        manual_allow_fallback=True,
                        allowed_provider_ids=[no_tools_provider["id"], glm_provider["id"]],
                    ),
                    require_tools=True,
                    require_chat_completions=True,
                )
                _assert(len(candidates) == 1, f"tool routing should keep exactly one glm candidate: {[(item.provider.name, item.provider_model.model_name) for item in candidates]}")
                _assert(candidates[0].provider.id == glm_provider["id"], f"tool routing selected wrong provider: {candidates[0].provider.id}")

                glm_provider_record = db.scalar(select(Provider).where(Provider.id == glm_provider["id"]))
                _assert(glm_provider_record is not None, "glm provider record missing")
                glm_provider_model = next((item for item in glm_provider_record.provider_models if item.model_name == "glm-5.1"), None)
                _assert(glm_provider_model is not None, "glm provider model missing in database")
                tool_probe = client.portal.call(HealthService._probe_native_tools, glm_provider_record, glm_provider_model)
                _assert(tool_probe["success"] is True, f"native tools probe should succeed: {tool_probe}")
                _assert(tool_probe["support_mode"] == "native", f"native tools probe mode mismatch: {tool_probe}")

            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={
                    "model": "glm-5.1",
                    "messages": [{"role": "user", "content": "请调用 get_weather 工具"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "description": "Get current weather",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                },
                            },
                        }
                    ],
                    "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
                },
            )
            _assert(response.status_code == 200, f"glm tool request failed: {response.status_code} {response.text}")
            body = response.json()
            choice = body.get("choices", [{}])[0]
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            _assert(choice.get("finish_reason") == "tool_calls", f"finish_reason should be tool_calls: {body}")
            _assert(bool(message.get("tool_calls")), f"tool_calls missing in response body: {body}")


if __name__ == "__main__":
    main()
