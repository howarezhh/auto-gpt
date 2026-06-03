from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage19-completions-regression.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage19-completions-regression.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ASYNC_REQUEST_LOG_ENABLED"] = "false"

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.services.proxy_service import ProxyService
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


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
        if UserAuthService.get_user_by_login(db, "stage19-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage19-admin",
                email="stage19-admin@example.com",
                password="Stage19Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage19-admin", password="Stage19Admin#123")


def _create_provider(client: TestClient) -> dict:
    response = client.post(
        "/api/providers",
        json={
            "name": "stage19-completions-provider",
            "base_url": "https://example.com/v1",
            "api_key": "upstream-secret",
            "provider_type": "openai_compatible",
            "enabled": True,
            "priority": 10,
            "weight": 100,
            "timeout_ms": 30000,
            "max_retries": 1,
            "model_configs": [
                {
                    "model_name": "stage19-compatible-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": False,
                    "enabled": True,
                }
            ],
            "remark": "stage19 completions offline provider",
        },
    )
    _assert(response.status_code == 201, f"create provider failed: {response.text}")
    return response.json()


def _create_api_key(
    client: TestClient,
    *,
    provider_id: int,
    name: str,
    allowed_endpoint_paths: list[str] | None = None,
) -> dict:
    payload = {
        "name": name,
        "remark": "stage19 completions key",
        "enabled": True,
        "token_limit_total": 5000,
        "route_mode": "failover",
        "default_provider_id": provider_id,
        "manual_allow_fallback": True,
        "allowed_provider_ids": [provider_id],
    }
    if allowed_endpoint_paths is not None:
        payload["allowed_endpoint_paths"] = allowed_endpoint_paths
    response = client.post("/api/api-keys", json=payload)
    _assert(response.status_code == 201, f"create api key failed: {response.text}")
    return response.json()


async def _fake_forward_json_request(**kwargs):
    _assert(kwargs["endpoint_path"] == "/completions", f"wrong endpoint: {kwargs}")
    _assert(kwargs["log_type"] == "chat", f"wrong log type: {kwargs}")
    payload = kwargs["payload"]
    _assert(payload["prompt"] == "阶段19提示词", f"prompt not preserved: {payload}")
    _assert(payload["metadata"] == {"扩展": "保留"}, f"metadata not preserved: {payload}")
    return (
        {
            "id": "cmpl_stage19",
            "object": "text_completion",
            "model": payload["model"],
            "choices": [{"text": "阶段19回答", "index": 0, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        },
        SimpleNamespace(id=19, name="stage19-completions-provider"),
        [{"result": "success", "latency_ms": 1, "status_code": 200}],
        1,
    )


async def _fake_forward_stream_request(**kwargs):
    _assert(kwargs["endpoint_path"] == "/completions", f"wrong stream endpoint: {kwargs}")
    _assert(kwargs["payload"]["stream"] is True, f"stream flag not preserved: {kwargs}")

    async def _stream():
        yield b'data: {"id":"cmpl_stage19","choices":[{"text":"A","index":0}]}\n\n'
        yield b"data: [DONE]\n\n"

    return (
        _stream(),
        SimpleNamespace(id=19, name="stage19-completions-provider"),
        [{"result": "stream_opened", "latency_ms": 1, "status_code": 200}],
        1,
    )


def main() -> None:
    with TestClient(app) as client:
        _bootstrap_admin(client)
        provider = _create_provider(client)
        api_key = _create_api_key(client, provider_id=provider["id"], name="stage19-open-key")
        restricted_key = _create_api_key(
            client,
            provider_id=provider["id"],
            name="stage19-chat-only-key",
            allowed_endpoint_paths=["/v1/chat/completions"],
        )

        with patch.object(ProxyService, "forward_json_request", side_effect=_fake_forward_json_request):
            response = client.post(
                "/v1/completions",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={
                    "model": "stage19-compatible-model",
                    "prompt": "阶段19提示词",
                    "metadata": {"扩展": "保留"},
                },
            )
        _assert(response.status_code == 200, f"completion status mismatch: {response.text}")
        body = response.json()
        _assert(body["object"] == "text_completion", f"completion body mismatch: {body}")
        _assert(response.headers.get("x-proxy-provider-name") == "stage19-completions-provider", "missing provider header")

        with patch.object(ProxyService, "forward_stream_request", side_effect=_fake_forward_stream_request):
            with client.stream(
                "POST",
                "/v1/completions",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={"model": "stage19-compatible-model", "prompt": "阶段19提示词", "stream": True},
            ) as stream_response:
                _assert(stream_response.status_code == 200, f"stream status mismatch: {stream_response.status_code}")
                stream_text = "".join(stream_response.iter_text())
        _assert('"cmpl_stage19"' in stream_text, f"stream chunk missing: {stream_text}")
        _assert("data: [DONE]" in stream_text, f"stream done missing: {stream_text}")

        forbidden = client.post(
            "/v1/completions",
            headers={"Authorization": f"Bearer {restricted_key['raw_api_key']}"},
            json={"model": "stage19-compatible-model", "prompt": "blocked"},
        )
        _assert(forbidden.status_code == 403, f"endpoint restriction not enforced: {forbidden.text}")
        _assert(forbidden.json()["error"]["code"] == "endpoint_not_allowed", f"unexpected auth error: {forbidden.text}")

        options = client.options(
            "/v1/responses/resp_stage19",
            headers={"Origin": "https://client.example", "Access-Control-Request-Method": "DELETE"},
        )
        _assert(options.status_code == 204, f"options status mismatch: {options.text}")
        _assert("DELETE" in options.headers.get("access-control-allow-methods", ""), f"DELETE not allowed in CORS: {options.headers}")

        unsupported = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
            json={"model": "stage19-compatible-model"},
        )
        supported_endpoints = unsupported.json()["error"]["detail"]["supported_endpoints"]
        _assert("POST /v1/completions" in supported_endpoints, f"completions missing in supported list: {supported_endpoints}")

    print("stage19 completions regression passed")


if __name__ == "__main__":
    main()
