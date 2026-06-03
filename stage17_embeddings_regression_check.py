from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage17-embeddings-regression.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage17-embeddings-regression.db"
os.environ["ENABLE_SCHEDULER"] = "false"

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
        if UserAuthService.get_user_by_login(db, "stage17-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage17-admin",
                email="stage17-admin@example.com",
                password="Stage17Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage17-admin", password="Stage17Admin#123")


def _create_provider(client: TestClient) -> dict:
    response = client.post(
        "/api/providers",
        json={
            "name": "stage17-embedding-provider",
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
                    "model_name": "stage17-compatible-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": False,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": False,
                    "supports_responses": False,
                    "enabled": True,
                }
            ],
            "remark": "stage17 embeddings offline provider",
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
        "remark": "stage17 embeddings key",
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
    _assert(kwargs["endpoint_path"] == "/embeddings", f"wrong endpoint: {kwargs}")
    _assert(kwargs["log_type"] == "embeddings", f"wrong log type: {kwargs}")
    _assert(kwargs["payload"]["model"] == "stage17-compatible-model", f"wrong model: {kwargs}")
    return (
        {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": [0.1, 0.2, 0.3],
                }
            ],
            "model": "stage17-compatible-model",
            "usage": {
                "prompt_tokens": 3,
                "total_tokens": 3,
            },
        },
        SimpleNamespace(id=17, name="stage17-embedding-provider"),
        [{"result": "success", "latency_ms": 1, "status_code": 200}],
        1,
    )


def main() -> None:
    with TestClient(app) as client:
        _bootstrap_admin(client)
        provider = _create_provider(client)
        api_key = _create_api_key(client, provider_id=provider["id"], name="stage17-embeddings-open")

        with patch.object(ProxyService, "forward_json_request", side_effect=_fake_forward_json_request):
            response = client.post(
                "/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
                json={"model": "stage17-compatible-model", "input": ["你好", "世界"]},
            )
        _assert(response.status_code == 200, f"embeddings status mismatch: {response.text}")
        body = response.json()
        _assert(body["object"] == "list", f"unexpected body: {body}")
        _assert(body["data"][0]["object"] == "embedding", f"unexpected embedding item: {body}")
        _assert(response.headers.get("x-proxy-provider-name") == "stage17-embedding-provider", "missing provider header")

        restricted_key = _create_api_key(
            client,
            provider_id=provider["id"],
            name="stage17-embeddings-restricted",
            allowed_endpoint_paths=["/v1/responses"],
        )
        forbidden = client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {restricted_key['raw_api_key']}"},
            json={"model": "stage17-compatible-model", "input": "blocked"},
        )
        _assert(forbidden.status_code == 403, f"endpoint restriction not enforced: {forbidden.text}")
        _assert(forbidden.json()["error"]["code"] == "endpoint_not_allowed", f"unexpected auth error: {forbidden.text}")

        model_detail = client.get(
            "/v1/models/stage17-compatible-model",
            headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
        )
        _assert(model_detail.status_code == 200, f"model detail status mismatch: {model_detail.text}")
        _assert(model_detail.json()["id"] == "stage17-compatible-model", f"model detail body mismatch: {model_detail.text}")

        missing_model = client.get(
            "/v1/models/not-mounted-model",
            headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
        )
        _assert(missing_model.status_code == 404, f"missing model status mismatch: {missing_model.text}")
        _assert(missing_model.json()["error"]["code"] == "model_not_found", f"missing model error mismatch: {missing_model.text}")

        unsupported = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key['raw_api_key']}"},
            json={"model": "stage17-compatible-model"},
        )
        _assert(unsupported.status_code == 404, f"unsupported endpoint status mismatch: {unsupported.text}")
        supported_endpoints = unsupported.json()["error"]["detail"]["supported_endpoints"]
        _assert("POST /v1/embeddings" in supported_endpoints, f"embeddings missing in supported list: {supported_endpoints}")
        _assert("GET /v1/models/{model}" in supported_endpoints, f"model retrieve missing in supported list: {supported_endpoints}")

    print("stage17 embeddings regression passed")


if __name__ == "__main__":
    main()
