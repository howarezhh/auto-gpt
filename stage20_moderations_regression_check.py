from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage20-moderations-regression.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test")
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ASYNC_REQUEST_LOG_ENABLED"] = "false"

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models.request_log import RequestLog
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
        if UserAuthService.get_user_by_login(db, "stage20-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage20-admin",
                email="stage20-admin@example.com",
                password="Stage20Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage20-admin", password="Stage20Admin#123")


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
                    "model_name": "stage20-compatible-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": False,
                    "supports_vision": True,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": False,
                    "enabled": True,
                }
            ],
            "remark": f"stage20 {name}",
        },
    )
    _assert(response.status_code == 201, f"create provider failed: {response.text}")
    return response.json()


def _create_api_key(
    client: TestClient,
    *,
    default_provider_id: int,
    allowed_provider_ids: list[int],
    name: str,
    allowed_endpoint_paths: list[str] | None = None,
) -> dict:
    payload = {
        "name": name,
        "remark": "stage20 moderations key",
        "enabled": True,
        "token_limit_total": 5000,
        "route_mode": "failover",
        "default_provider_id": default_provider_id,
        "manual_allow_fallback": True,
        "allowed_provider_ids": allowed_provider_ids,
    }
    if allowed_endpoint_paths is not None:
        payload["allowed_endpoint_paths"] = allowed_endpoint_paths
    response = client.post("/api/api-keys", json=payload)
    _assert(response.status_code == 201, f"create api key failed: {response.text}")
    return response.json()


async def _fake_send_management_request(provider, *, method: str, request_path: str, query_items, payload=None):
    CAPTURED_CALLS.append(
        {
            "provider_name": provider.name,
            "method": method,
            "request_path": request_path,
            "query_items": list(query_items or []),
            "payload": payload,
        }
    )
    if provider.name == "stage20-primary":
        raise RequestsUpstreamHTTPError(
            status_code=404,
            detail={"message": "moderations endpoint missing", "code": "moderation_not_found"},
        )
    return {
        "id": "modr_stage20",
        "model": payload.get("model") if isinstance(payload, dict) else None,
        "results": [
            {
                "flagged": False,
                "categories": {"violence": False},
                "category_scores": {"violence": 0.0},
            }
        ],
    }, "upstream-stage20-moderation"


def main() -> None:
    with patch.object(ProxyService, "_send_response_management_request", side_effect=_fake_send_management_request):
        with TestClient(app) as client:
            _bootstrap_admin(client)
            primary = _create_provider(client, name="stage20-primary", priority=10)
            secondary = _create_provider(client, name="stage20-secondary", priority=20)
            api_key = _create_api_key(
                client,
                default_provider_id=primary["id"],
                allowed_provider_ids=[primary["id"], secondary["id"]],
                name="stage20-open-key",
            )
            restricted_key = _create_api_key(
                client,
                default_provider_id=primary["id"],
                allowed_provider_ids=[primary["id"], secondary["id"]],
                name="stage20-chat-only-key",
                allowed_endpoint_paths=["/v1/chat/completions"],
            )

            payload = {
                "model": "stage20-compatible-model",
                "input": [
                    {
                        "type": "text",
                        "text": "这是一条需要审核的文本",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
                "metadata": {"扩展字段": "必须透传"},
            }
            response = client.post(
                "/v1/moderations",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json=payload,
            )
            _assert(response.status_code == 200, f"moderation status mismatch: {response.text}")
            body = response.json()
            _assert(body["id"] == "modr_stage20", f"moderation body mismatch: {body}")
            _assert(body["model"] == "stage20-compatible-model", f"moderation model mismatch: {body}")
            _assert(response.headers.get("x-proxy-provider-name") == "stage20-secondary", "missing selected provider header")

            forbidden = client.post(
                "/v1/moderations",
                headers={"Authorization": f"Bearer {restricted_key['raw_api_key']}"},
                json=payload,
            )
            _assert(forbidden.status_code == 403, f"endpoint restriction not enforced: {forbidden.text}")
            _assert(forbidden.json()["error"]["code"] == "endpoint_not_allowed", f"unexpected auth error: {forbidden.text}")

            unsupported = client.post(
                "/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={"model": "stage20-compatible-model"},
            )
            supported_endpoints = unsupported.json()["error"]["detail"]["supported_endpoints"]
            _assert("POST /v1/moderations" in supported_endpoints, f"moderations missing in supported list: {supported_endpoints}")

    _assert(len(CAPTURED_CALLS) == 2, f"moderation should fail over across two providers: {CAPTURED_CALLS}")
    _assert(all(item["request_path"] == "/moderations" for item in CAPTURED_CALLS), f"wrong path: {CAPTURED_CALLS}")
    _assert(CAPTURED_CALLS[0]["payload"]["metadata"] == {"扩展字段": "必须透传"}, f"payload not preserved: {CAPTURED_CALLS}")

    with SessionLocal() as db:
        logs = (
            db.query(RequestLog)
            .filter(RequestLog.log_type == "moderations")
            .filter(RequestLog.request_path == "/v1/moderations")
            .order_by(RequestLog.id.asc())
            .all()
        )
        _assert(len(logs) == 1, f"moderation should create one log: {logs}")
        log = logs[0]
        _assert(log.success is True, f"moderation log should be successful: {log}")
        _assert(log.model_name == "stage20-compatible-model", f"moderation log should record model: {log.model_name}")
        _assert(log.provider_id is not None and log.provider_name == "stage20-secondary", f"log should record provider: {log}")
        _assert(log.api_client_key_id is not None, f"log should include API Key context: {log}")
        _assert(log.attempt_count == 2, f"log should include failover attempts: {log.attempt_count}")

    print("stage20 moderations regression passed")


if __name__ == "__main__":
    main()
