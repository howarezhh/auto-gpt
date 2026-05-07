from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import httpx


TEMP_DB_PATH = Path("data/stage12-error-response.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage12-error-response.db"
os.environ["ENABLE_SCHEDULER"] = "false"

from fastapi import HTTPException
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
        if UserAuthService.get_user_by_login(db, "stage12-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage12-admin",
                email="stage12-admin@example.com",
                password="Stage12Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage12-admin", password="Stage12Admin#123")


def _create_provider(client: TestClient, *, name: str, priority: int) -> dict:
    response = client.post(
        "/api/providers",
        json={
            "name": name,
            "base_url": "https://example.com/v1",
            "api_key": "upstream-secret",
            "provider_type": "openai_compatible",
            "enabled": True,
            "priority": priority,
            "weight": 100,
            "timeout_ms": 30000,
            "max_retries": 1,
            "model_configs": [
                {
                    "model_name": "stage12-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": True,
                    "enabled": True,
                }
            ],
            "remark": f"stage12 {name}",
        },
    )
    _assert(response.status_code == 201, f"create provider failed: {response.text}")
    return response.json()


def _create_api_key(client: TestClient, *, default_provider_id: int, allowed_provider_ids: list[int]) -> dict:
    response = client.post(
        "/api/api-keys",
        json={
            "name": "stage12-key",
            "remark": "stage12 error response key",
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


async def _raise_connect_error(*args, **kwargs):
    raise httpx.ConnectError(
        "upstream connect failed",
        request=httpx.Request("POST", "https://example.com/v1/responses"),
    )


async def _fake_stream_request(*args, **kwargs):
    async def _stream():
        raise HTTPException(
            status_code=504,
            detail={"message": "upstream read timeout", "code": "upstream_read_timeout"},
        )
        yield b""

    provider = type("ProviderStub", (), {"id": 999, "name": "stage12-stream-provider"})()
    trace = [{"result": "stream_opened", "latency_ms": 1, "status_code": 200}]
    return _stream(), provider, trace, 1


def main() -> None:
    with TestClient(app) as client:
        _bootstrap_admin(client)
        primary = _create_provider(client, name="stage12-primary", priority=10)
        secondary = _create_provider(client, name="stage12-secondary", priority=20)
        api_key = _create_api_key(
            client,
            default_provider_id=primary["id"],
            allowed_provider_ids=[primary["id"], secondary["id"]],
        )

        with patch.object(ProxyService, "_forward_json_with_endpoint_fallback", side_effect=_raise_connect_error):
            non_stream_response = client.post(
                "/v1/responses",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={"model": "stage12-model", "input": "ping"},
            )
        _assert(non_stream_response.status_code == 502, f"unexpected status: {non_stream_response.text}")
        non_stream_body = non_stream_response.json()
        _assert(non_stream_body["error"]["code"] == "all_providers_failed", f"unexpected code: {non_stream_body}")
        _assert(non_stream_body["error"]["detail"]["last_error"]["code"] == "upstream_connect_error", f"missing last error code: {non_stream_body}")
        _assert(non_stream_body["error"]["detail"]["attempt_count"] == 2, f"attempt count mismatch: {non_stream_body}")
        _assert(non_stream_body["error"]["retryable"] is True, f"retryable mismatch: {non_stream_body}")

        with patch.object(ProxyService, "forward_stream_request", side_effect=_fake_stream_request):
            with client.stream(
                "POST",
                "/v1/responses",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={"model": "stage12-model", "input": "ping", "stream": True},
            ) as stream_response:
                _assert(stream_response.status_code == 200, f"stream open failed: {stream_response.status_code}")
                stream_text = "".join(stream_response.iter_text())
        _assert("event: error" in stream_text, f"missing error event: {stream_text}")
        _assert("\"code\": \"upstream_read_timeout\"" in stream_text, f"missing stream error code: {stream_text}")
        _assert("data: [DONE]" in stream_text, f"missing done marker: {stream_text}")

    print("stage12 error response regression passed")


if __name__ == "__main__":
    main()
