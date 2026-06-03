from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage21-files-regression.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage21-files-regression.db"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ASYNC_REQUEST_LOG_ENABLED"] = "false"

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models.request_log import RequestLog
from app.services.proxy_service import ProxyService, RequestsUpstreamHTTPError
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


CAPTURED_JSON_CALLS: list[dict] = []
CAPTURED_RAW_CALLS: list[dict] = []


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
        if UserAuthService.get_user_by_login(db, "stage21-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage21-admin",
                email="stage21-admin@example.com",
                password="Stage21Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage21-admin", password="Stage21Admin#123")


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
                    "model_name": "stage21-compatible-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": False,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": False,
                    "enabled": True,
                }
            ],
            "remark": f"stage21 {name}",
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
        "remark": "stage21 files key",
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


async def _fake_send_management_request(
    provider,
    *,
    method: str,
    request_path: str,
    query_items,
    payload=None,
    form_fields=None,
    form_files=None,
):
    CAPTURED_JSON_CALLS.append(
        {
            "provider_name": provider.name,
            "method": method,
            "request_path": request_path,
            "query_items": list(query_items or []),
            "payload": payload,
            "form_fields": list(form_fields or []),
            "form_files": [
                {
                    "field": field,
                    "filename": file_tuple[0],
                    "content": file_tuple[1],
                    "content_type": file_tuple[2],
                }
                for field, file_tuple in (form_files or [])
            ],
        }
    )
    if provider.name == "stage21-primary":
        raise RequestsUpstreamHTTPError(
            status_code=404,
            detail={"message": "file endpoint not found", "code": "file_not_found"},
        )
    if request_path == "/files" and method == "GET":
        return {
            "object": "list",
            "data": [{"id": "file_stage21", "object": "file", "purpose": "assistants"}],
            "has_more": False,
        }, "upstream-stage21-list"
    if request_path == "/files" and method == "POST":
        return {
            "id": "file_stage21",
            "object": "file",
            "purpose": dict(form_fields or []).get("purpose"),
            "filename": (form_files or [("file", ("", b"", ""))])[0][1][0],
        }, "upstream-stage21-upload"
    if method == "DELETE":
        return {"id": "file_stage21", "object": "file", "deleted": True}, "upstream-stage21-delete"
    return {
        "id": "file_stage21",
        "object": "file",
        "bytes": 18,
        "purpose": "assistants",
    }, "upstream-stage21-retrieve"


async def _fake_send_raw_management_request(provider, *, method: str, request_path: str, query_items):
    CAPTURED_RAW_CALLS.append(
        {
            "provider_name": provider.name,
            "method": method,
            "request_path": request_path,
            "query_items": list(query_items or []),
        }
    )
    if provider.name == "stage21-primary":
        raise RequestsUpstreamHTTPError(
            status_code=404,
            detail={"message": "file content not found", "code": "file_content_not_found"},
        )
    return b'{"hello":"stage21"}\n', "application/jsonl", "upstream-stage21-content"


def main() -> None:
    with (
        patch.object(ProxyService, "_send_response_management_request", side_effect=_fake_send_management_request),
        patch.object(ProxyService, "_send_raw_management_request", side_effect=_fake_send_raw_management_request),
    ):
        with TestClient(app) as client:
            _bootstrap_admin(client)
            primary = _create_provider(client, name="stage21-primary", priority=10)
            secondary = _create_provider(client, name="stage21-secondary", priority=20)
            api_key = _create_api_key(
                client,
                default_provider_id=primary["id"],
                allowed_provider_ids=[primary["id"], secondary["id"]],
                name="stage21-open-key",
            )
            files_only_key = _create_api_key(
                client,
                default_provider_id=primary["id"],
                allowed_provider_ids=[primary["id"], secondary["id"]],
                name="stage21-files-only-key",
                allowed_endpoint_paths=["/v1/files"],
            )
            chat_only_key = _create_api_key(
                client,
                default_provider_id=primary["id"],
                allowed_provider_ids=[primary["id"], secondary["id"]],
                name="stage21-chat-only-key",
                allowed_endpoint_paths=["/v1/chat/completions"],
            )

            list_response = client.get(
                "/v1/files",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                params=[("purpose", "assistants")],
            )
            _assert(list_response.status_code == 200, f"list files failed: {list_response.text}")
            _assert(list_response.json()["data"][0]["id"] == "file_stage21", f"list body mismatch: {list_response.text}")

            upload_response = client.post(
                "/v1/files",
                headers={"Authorization": f"Bearer {files_only_key['raw_api_key']}"},
                data={"purpose": "assistants", "metadata[业务]": "阶段21"},
                files={"file": ("payload.jsonl", b'{"hello":"stage21"}\n', "application/jsonl")},
            )
            _assert(upload_response.status_code == 200, f"upload file failed: {upload_response.text}")
            _assert(upload_response.json()["filename"] == "payload.jsonl", f"upload body mismatch: {upload_response.text}")

            retrieve_response = client.get(
                "/v1/files/file_stage21",
                headers={"Authorization": f"Bearer {files_only_key['raw_api_key']}"},
            )
            _assert(retrieve_response.status_code == 200, f"retrieve file failed: {retrieve_response.text}")
            _assert(retrieve_response.json()["purpose"] == "assistants", f"retrieve body mismatch: {retrieve_response.text}")

            content_response = client.get(
                "/v1/files/file_stage21/content",
                headers={"Authorization": f"Bearer {files_only_key['raw_api_key']}"},
            )
            _assert(content_response.status_code == 200, f"content failed: {content_response.text}")
            _assert(content_response.content == b'{"hello":"stage21"}\n', f"content mismatch: {content_response.content!r}")
            _assert(content_response.headers["content-type"].startswith("application/jsonl"), f"content type mismatch: {content_response.headers}")

            delete_response = client.delete(
                "/v1/files/file_stage21",
                headers={"Authorization": f"Bearer {files_only_key['raw_api_key']}"},
            )
            _assert(delete_response.status_code == 200, f"delete file failed: {delete_response.text}")
            _assert(delete_response.json()["deleted"] is True, f"delete body mismatch: {delete_response.text}")

            forbidden = client.get(
                "/v1/files",
                headers={"Authorization": f"Bearer {chat_only_key['raw_api_key']}"},
            )
            _assert(forbidden.status_code == 403, f"endpoint restriction not enforced: {forbidden.text}")
            _assert(forbidden.json()["error"]["code"] == "endpoint_not_allowed", f"unexpected auth error: {forbidden.text}")

            unsupported = client.post(
                "/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={"model": "stage21-compatible-model"},
            )
            supported_endpoints = unsupported.json()["error"]["detail"]["supported_endpoints"]
            _assert("GET /v1/files/{file_id}/content" in supported_endpoints, f"files content missing: {supported_endpoints}")
            _assert("POST /v1/files" in supported_endpoints, f"files upload missing: {supported_endpoints}")

    json_paths = [item["request_path"] for item in CAPTURED_JSON_CALLS]
    raw_paths = [item["request_path"] for item in CAPTURED_RAW_CALLS]
    _assert(json_paths.count("/files") == 4, f"list/upload should each fail over: {CAPTURED_JSON_CALLS}")
    _assert(json_paths.count("/files/file_stage21") == 4, f"retrieve/delete should each fail over: {CAPTURED_JSON_CALLS}")
    _assert(raw_paths.count("/files/file_stage21/content") == 2, f"content should fail over: {CAPTURED_RAW_CALLS}")
    upload_calls = [item for item in CAPTURED_JSON_CALLS if item["request_path"] == "/files" and item["method"] == "POST"]
    _assert(upload_calls[0]["form_fields"] == [("purpose", "assistants"), ("metadata[业务]", "阶段21")], f"form fields not preserved: {upload_calls}")
    _assert(upload_calls[0]["form_files"][0]["content"] == b'{"hello":"stage21"}\n', f"file content not forwarded: {upload_calls}")

    with SessionLocal() as db:
        logs = (
            db.query(RequestLog)
            .filter(RequestLog.log_type == "files")
            .filter(RequestLog.request_path.like("/v1/files%"))
            .order_by(RequestLog.id.asc())
            .all()
        )
        _assert(len(logs) == 5, f"files should create five logs: {logs}")
        _assert(all(item.success for item in logs), f"files logs should be successful: {logs}")
        _assert({item.http_method for item in logs} == {"GET", "POST", "DELETE"}, f"logs should record methods: {logs}")
        _assert(all(item.provider_id is not None for item in logs), f"logs should record provider: {logs}")
        _assert(all(item.api_client_key_id is not None for item in logs), f"logs should include API Key context: {logs}")
        _assert(all(item.attempt_count == 2 for item in logs), f"logs should include failover attempts: {logs}")

    print("stage21 files regression passed")


if __name__ == "__main__":
    main()
