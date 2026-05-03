from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage11-responses-management.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage11-responses-management.db"

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.services.proxy_service import ProxyService, RequestsUpstreamHTTPError
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


CAPTURED_CALLS: list[dict] = []


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
        if UserAuthService.get_user_by_login(db, "stage11-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage11-admin",
                email="stage11-admin@example.com",
                password="Stage11Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage11-admin", password="Stage11Admin#123")


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
                    "model_name": "stage11-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": True,
                    "supports_chat_completions": True,
                    "supports_responses": True,
                    "enabled": True,
                }
            ],
            "remark": f"stage11 {name}",
        },
    )
    _assert(response.status_code == 201, f"create provider failed: {response.text}")
    return response.json()


def _create_api_key(client: TestClient, *, default_provider_id: int, allowed_provider_ids: list[int]) -> dict:
    response = client.post(
        "/api/api-keys",
        json={
            "name": "stage11-key",
            "remark": "stage11 responses management key",
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


async def _fake_send_response_management_request(provider, *, method: str, request_path: str, query_items):
    CAPTURED_CALLS.append(
        {
            "provider_name": provider.name,
            "method": method,
            "request_path": request_path,
            "query_items": list(query_items or []),
        }
    )
    if provider.name == "stage11-primary":
        raise RequestsUpstreamHTTPError(
            status_code=404,
            detail={"message": "response not found", "code": "response_not_found"},
        )
    if method == "GET":
        return {
            "id": "resp_stage11",
            "object": "response",
            "status": "completed",
            "model": "stage11-model",
            "output_text": "hello from secondary",
        }, "upstream-stage11-get"
    return {
        "id": "resp_stage11",
        "object": "response",
        "status": "cancelled",
        "model": "stage11-model",
    }, "upstream-stage11-cancel"


def main() -> None:
    with patch.object(ProxyService, "_send_response_management_request", side_effect=_fake_send_response_management_request):
        with TestClient(app) as client:
            _bootstrap_admin(client)
            primary = _create_provider(client, name="stage11-primary", priority=10)
            secondary = _create_provider(client, name="stage11-secondary", priority=20)
            api_key = _create_api_key(
                client,
                default_provider_id=primary["id"],
                allowed_provider_ids=[primary["id"], secondary["id"]],
            )

            retrieve_response = client.get(
                "/v1/responses/resp_stage11",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                params=[("include", "output[0].content[0].text")],
            )
            _assert(retrieve_response.status_code == 200, f"retrieve response failed: {retrieve_response.text}")
            retrieve_body = retrieve_response.json()
            _assert(retrieve_body["status"] == "completed", f"retrieve body mismatch: {retrieve_body}")
            _assert(
                retrieve_response.headers.get("X-Proxy-Provider-Id") == str(secondary["id"]),
                f"retrieve should fall through to secondary provider: {retrieve_response.headers}",
            )

            cancel_response = client.post(
                "/v1/responses/resp_stage11/cancel",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
            )
            _assert(cancel_response.status_code == 200, f"cancel response failed: {cancel_response.text}")
            cancel_body = cancel_response.json()
            _assert(cancel_body["status"] == "cancelled", f"cancel body mismatch: {cancel_body}")
            _assert(
                cancel_response.headers.get("X-Proxy-Provider-Id") == str(secondary["id"]),
                f"cancel should fall through to secondary provider: {cancel_response.headers}",
            )

    retrieve_calls = [item for item in CAPTURED_CALLS if item["method"] == "GET"]
    cancel_calls = [item for item in CAPTURED_CALLS if item["method"] == "POST"]
    _assert(len(retrieve_calls) == 2, f"retrieve should try two providers: {retrieve_calls}")
    _assert(len(cancel_calls) == 2, f"cancel should try two providers: {cancel_calls}")
    _assert(
        retrieve_calls[0]["query_items"] == [("include", "output[0].content[0].text")],
        f"retrieve query params should be forwarded: {retrieve_calls}",
    )
    _assert(
        all(item["request_path"].startswith("/responses/resp_stage11") for item in CAPTURED_CALLS),
        f"unexpected request path: {CAPTURED_CALLS}",
    )

    print("stage11 responses management regression passed")


if __name__ == "__main__":
    main()
