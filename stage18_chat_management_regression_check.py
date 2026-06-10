from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage18-chat-management.db")
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
        if UserAuthService.get_user_by_login(db, "stage18-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage18-admin",
                email="stage18-admin@example.com",
                password="Stage18Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage18-admin", password="Stage18Admin#123")


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
                    "model_name": "stage18-chat-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": True,
                    "supports_chat_completions": True,
                    "supports_responses": False,
                    "enabled": True,
                }
            ],
            "remark": f"stage18 {name}",
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
        "remark": "stage18 chat management key",
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
    if provider.name == "stage18-primary":
        raise RequestsUpstreamHTTPError(
            status_code=404,
            detail={"message": "chat completion not found", "code": "chat_completion_not_found"},
        )
    if request_path == "/chat/completions":
        return {
            "object": "list",
            "data": [{"id": "chatcmpl_stage18", "object": "chat.completion"}],
            "has_more": False,
        }, "upstream-stage18-list"
    if request_path.endswith("/messages"):
        return {
            "object": "list",
            "data": [{"id": "msg_stage18", "role": "assistant", "content": "hello"}],
            "has_more": False,
        }, "upstream-stage18-messages"
    if method == "POST":
        return {
            "id": "chatcmpl_stage18",
            "object": "chat.completion",
            "metadata": payload.get("metadata") if isinstance(payload, dict) else None,
        }, "upstream-stage18-update"
    if method == "DELETE":
        return {
            "id": "chatcmpl_stage18",
            "object": "chat.completion.deleted",
            "deleted": True,
        }, "upstream-stage18-delete"
    return {
        "id": "chatcmpl_stage18",
        "object": "chat.completion",
        "model": "stage18-chat-model",
    }, "upstream-stage18-retrieve"


def main() -> None:
    with patch.object(ProxyService, "_send_response_management_request", side_effect=_fake_send_management_request):
        with TestClient(app) as client:
            _bootstrap_admin(client)
            primary = _create_provider(client, name="stage18-primary", priority=10)
            secondary = _create_provider(client, name="stage18-secondary", priority=20)
            api_key = _create_api_key(
                client,
                default_provider_id=primary["id"],
                allowed_provider_ids=[primary["id"], secondary["id"]],
                name="stage18-open-key",
            )
            restricted_key = _create_api_key(
                client,
                default_provider_id=primary["id"],
                allowed_provider_ids=[primary["id"], secondary["id"]],
                name="stage18-chat-only-key",
                allowed_endpoint_paths=["/v1/chat/completions"],
            )

            list_response = client.get(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                params=[("limit", "10")],
            )
            _assert(list_response.status_code == 200, f"list failed: {list_response.text}")
            _assert(list_response.json()["data"][0]["id"] == "chatcmpl_stage18", f"list body mismatch: {list_response.text}")

            retrieve_response = client.get(
                "/v1/chat/completions/chatcmpl_stage18",
                headers={"Authorization": f"Bearer {restricted_key['raw_api_key']}"},
            )
            _assert(retrieve_response.status_code == 200, f"retrieve failed: {retrieve_response.text}")
            _assert(retrieve_response.json()["model"] == "stage18-chat-model", f"retrieve body mismatch: {retrieve_response.text}")

            messages_response = client.get(
                "/v1/chat/completions/chatcmpl_stage18/messages",
                headers={"Authorization": f"Bearer {restricted_key['raw_api_key']}"},
                params=[("after", "msg_before")],
            )
            _assert(messages_response.status_code == 200, f"messages failed: {messages_response.text}")
            _assert(messages_response.json()["data"][0]["id"] == "msg_stage18", f"messages body mismatch: {messages_response.text}")

            update_response = client.post(
                "/v1/chat/completions/chatcmpl_stage18",
                headers={"Authorization": f"Bearer {restricted_key['raw_api_key']}"},
                json={"metadata": {"业务": "阶段18"}},
            )
            _assert(update_response.status_code == 200, f"update failed: {update_response.text}")
            _assert(update_response.json()["metadata"]["业务"] == "阶段18", f"update body mismatch: {update_response.text}")

            delete_response = client.delete(
                "/v1/chat/completions/chatcmpl_stage18",
                headers={"Authorization": f"Bearer {restricted_key['raw_api_key']}"},
            )
            _assert(delete_response.status_code == 200, f"delete failed: {delete_response.text}")
            _assert(delete_response.json()["deleted"] is True, f"delete body mismatch: {delete_response.text}")

            unsupported = client.post(
                "/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={"model": "stage18-chat-model"},
            )
            supported_endpoints = unsupported.json()["error"]["detail"]["supported_endpoints"]
            _assert("GET /v1/chat/completions/{completion_id}/messages" in supported_endpoints, f"chat messages missing: {supported_endpoints}")
            _assert("DELETE /v1/chat/completions/{completion_id}" in supported_endpoints, f"chat delete missing: {supported_endpoints}")

    list_calls = [item for item in CAPTURED_CALLS if item["request_path"] == "/chat/completions"]
    retrieve_calls = [item for item in CAPTURED_CALLS if item["request_path"] == "/chat/completions/chatcmpl_stage18" and item["method"] == "GET"]
    messages_calls = [item for item in CAPTURED_CALLS if item["request_path"].endswith("/messages")]
    update_calls = [item for item in CAPTURED_CALLS if item["method"] == "POST"]
    delete_calls = [item for item in CAPTURED_CALLS if item["method"] == "DELETE"]
    _assert(len(list_calls) == 2, f"list should try two providers: {list_calls}")
    _assert(len(retrieve_calls) == 2, f"retrieve should try two providers: {retrieve_calls}")
    _assert(len(messages_calls) == 2, f"messages should try two providers: {messages_calls}")
    _assert(len(update_calls) == 2, f"update should try two providers: {update_calls}")
    _assert(len(delete_calls) == 2, f"delete should try two providers: {delete_calls}")
    _assert(list_calls[0]["query_items"] == [("limit", "10")], f"list query not forwarded: {list_calls}")
    _assert(messages_calls[0]["query_items"] == [("after", "msg_before")], f"messages query not forwarded: {messages_calls}")
    _assert(update_calls[0]["payload"] == {"metadata": {"业务": "阶段18"}}, f"update payload not forwarded: {update_calls}")

    with SessionLocal() as db:
        logs = (
            db.query(RequestLog)
            .filter(RequestLog.log_type == "chat")
            .filter(RequestLog.request_path.like("/v1/chat/completions%"))
            .order_by(RequestLog.id.asc())
            .all()
        )
        _assert(len(logs) == 5, f"chat management should create five logs: {logs}")
        _assert(all(item.success for item in logs), f"chat management logs should be successful: {logs}")
        _assert({item.http_method for item in logs} == {"GET", "POST", "DELETE"}, f"logs should record methods: {logs}")
        _assert(all(item.provider_id is not None for item in logs), f"logs should record provider: {logs}")
        _assert(all(item.api_client_key_id is not None for item in logs), f"logs should include API Key context: {logs}")
        _assert(all(item.attempt_count == 2 for item in logs), f"logs should include failover attempts: {logs}")

    print("stage18 chat management regression passed")


if __name__ == "__main__":
    main()
