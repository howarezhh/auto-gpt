from __future__ import annotations

import os
from pathlib import Path


TEMP_DB_PATH = Path("data/stage9-regression.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage9-regression.db"
os.environ["ENABLE_SCHEDULER"] = "false"

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models.api_client_key import ApiClientKey
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.user_account import UserAccount
from app.services.api_key_service import ApiKeyService
from app.services.health_service import HealthService
from app.services.proxy_service import ProxyService
from app.services.router_service import RoutePolicyContext, RouterService
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
        if UserAuthService.get_user_by_login(db, "stage9-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage9-admin",
                email="stage9-admin@example.com",
                password="Stage9Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage9-admin", password="Stage9Admin#123")


def _create_user(client: TestClient, *, username: str, email: str, password: str) -> int:
    response = client.post(
        "/users/create",
        data={
            "username": username,
            "email": email,
            "password": password,
            "role": "user",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    _assert(response.status_code == 303, f"create user failed: {response.text}")
    with SessionLocal() as db:
        user = db.scalar(select(UserAccount).where(UserAccount.username == username))
        _assert(user is not None, f"user {username} missing")
        return user.id


def _create_provider(
    client: TestClient,
    *,
    name: str,
    model_name: str,
    supports_stream: bool,
    supports_vision: bool,
    priority: int,
) -> dict:
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
                    "model_name": model_name,
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": supports_stream,
                    "supports_vision": supports_vision,
                    "enabled": True,
                    "input_price_per_1k": 0.001,
                    "output_price_per_1k": 0.002,
                }
            ],
            "remark": f"stage9 {name}",
        },
    )
    _assert(response.status_code == 201, f"create provider failed: {response.text}")
    return response.json()


def _create_api_key(
    client: TestClient,
    *,
    name: str,
    allowed_provider_ids: list[int],
    default_provider_id: int,
    expires_at: str | None = None,
) -> dict:
    response = client.post(
        "/api/api-keys",
        json={
            "name": name,
            "remark": f"stage9 {name}",
            "enabled": True,
            "expires_at": expires_at,
            "token_limit_total": 5000,
            "route_mode": "failover",
            "default_provider_id": default_provider_id,
            "manual_allow_fallback": True,
            "allowed_provider_ids": allowed_provider_ids,
        },
    )
    _assert(response.status_code == 201, f"create api key failed: {response.text}")
    return response.json()


def _assert_stream_routing_uses_supported_provider(*, expected_provider_id: int) -> None:
    with SessionLocal() as db:
        candidates = RouterService.order_candidates(
            db,
            model_name="stream-model",
            route_context=RoutePolicyContext(
                route_mode="failover",
                default_provider_id=expected_provider_id,
                manual_allow_fallback=True,
                allowed_provider_ids=[expected_provider_id],
            ),
            require_stream=True,
        )
        _assert(
            len(candidates) == 1,
            f"stream route should leave exactly one candidate: {[(item.provider.id, item.provider_model.supports_stream) for item in candidates]}",
        )
        _assert(candidates[0].provider.id == expected_provider_id, f"unexpected stream provider selected: {candidates[0].provider.id}")
        _assert(candidates[0].provider_model.supports_stream is True, "stream route selected a non-stream provider")


def _assert_non_stream_model_is_filtered(*, provider_id: int) -> None:
    with SessionLocal() as db:
        candidates = RouterService.order_candidates(
            db,
            model_name="nonstream-model",
            route_context=RoutePolicyContext(
                route_mode="failover",
                default_provider_id=provider_id,
                manual_allow_fallback=True,
                allowed_provider_ids=[provider_id],
            ),
            require_stream=True,
        )
        _assert(candidates == [], f"non-stream model should be filtered when require_stream=true: {candidates}")


def _assert_last_used_touch_is_batched(raw_api_key: str, api_key_id: int) -> None:
    with SessionLocal() as db:
        api_key = db.get(ApiClientKey, api_key_id)
        _assert(api_key is not None, "api key missing before auth check")
        _assert(api_key.last_used_at is None, f"last_used_at should start empty: {api_key.last_used_at}")

    with SessionLocal() as db:
        auth = ApiKeyService.authenticate_request(db, f"Bearer {raw_api_key}")
        _assert(auth.api_client_key.id == api_key_id, "auth returned unexpected api key")
        db.expire_all()
        reloaded = db.get(ApiClientKey, api_key_id)
        _assert(reloaded is not None, "api key missing after auth")
        _assert(reloaded.last_used_at is None, "authenticate_request should not synchronously commit last_used_at")

    _assert(api_key_id in ApiKeyService._pending_last_used_ids, "api key should be queued for batched last_used refresh")
    ApiKeyService.flush_pending_last_used_touches()

    with SessionLocal() as db:
        flushed = db.get(ApiClientKey, api_key_id)
        _assert(flushed is not None and flushed.last_used_at is not None, "batched last_used flush did not persist")


def _assert_responses_image_native_passthrough(*, provider_id: int) -> None:
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        _assert(provider is not None, f"provider missing: {provider_id}")
        payload = {
            "model": "vision-model",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe"},
                        {"type": "input_image", "image_url": "https://example.com/image.png"},
                    ],
                }
            ],
            "previous_response_id": "resp_123",
            "reasoning": {"effort": "medium"},
            "store": False,
            "include": ["output_text"],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "vision_result",
                    "schema": {"type": "object", "properties": {}},
                }
            },
            "truncation": "disabled",
            "background": False,
        }
        normalized_payload = ProxyService._normalize_reasoning_request_payload(
            endpoint_path="/responses",
            payload=payload,
        )
        prepared = ProxyService._prepare_upstream_request(
            provider,
            endpoint_path="/responses",
            payload=normalized_payload,
        )

    _assert(prepared.request_path == "/responses", f"responses image request should stay on native endpoint: {prepared}")
    request_payload = dict(prepared.request_payload)
    _assert(request_payload.get("previous_response_id") == "resp_123", f"previous_response_id not preserved: {request_payload}")
    _assert(isinstance(request_payload.get("reasoning"), dict), f"reasoning not preserved: {request_payload}")
    _assert(request_payload.get("store") is False, f"store not preserved: {request_payload}")
    _assert(request_payload.get("include") == ["output_text"], f"include not preserved: {request_payload}")
    _assert(isinstance(request_payload.get("text"), dict), f"text config not preserved: {request_payload}")
    _assert(request_payload.get("truncation") == "disabled", f"truncation not preserved: {request_payload}")
    _assert(request_payload.get("background") is False, f"background not preserved: {request_payload}")


def _assert_crlf_sse_parsing() -> None:
    buffer = bytearray(b'data: {"id":"one"}\r\n\r\ndata: {"id":"two"}\r\n\r\n')
    events = ProxyService._consume_sse_event_texts(buffer)
    _assert(len(events) == 2, f"CRLF SSE parser should split two events: {events}")
    _assert(buffer == bytearray(), f"SSE buffer should be drained: {buffer!r}")


def _assert_chat_stream_to_responses_adapter() -> None:
    state = ProxyService._create_responses_stream_state(payload={"model": "stream-model"})
    chunk = (
        b'data: {"id":"chatcmpl-stage9","model":"stream-model","created":1717171717,"choices":[{"delta":{"content":"hello "}}]}\r\n\r\n'
        b'data: {"choices":[{"delta":{"content":"world"},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\r\n\r\n'
        b"data: [DONE]\r\n\r\n"
    )
    events = ProxyService._adapt_chat_stream_chunk_to_responses_events(
        chunk,
        state=state,
        requested_model="stream-model",
    )
    rendered = b"".join(events).decode("utf-8", errors="ignore")
    _assert("response.created" in rendered, f"missing response.created event: {rendered}")
    _assert("response.output_text.delta" in rendered, f"missing delta event: {rendered}")
    _assert("response.completed" in rendered, f"missing completed event: {rendered}")
    _assert('"output_text": "hello world"' in rendered, f"combined output text missing: {rendered}")


def _assert_stream_probe_payload_uses_true_flag() -> None:
    class _ProviderModelStub:
        model_name = "stream-model"
        supports_stream = True
        supports_vision = False

    payload = HealthService._build_chat_probe_payload(
        _ProviderModelStub(),
        vision_probe=False,
        stream_probe=True,
    )
    _assert(payload.get("stream") is True, f"stream probe payload should set stream=true: {payload}")


def _assert_shared_wallet_balance_rejected(client: TestClient, *, provider_id: int, owner_user_id: int) -> None:
    response = client.post(
        "/api/api-keys",
        json={
            "name": "stage9-shared-wallet",
            "remark": "stage9 shared wallet",
            "enabled": True,
            "token_limit_total": 1000,
            "route_mode": "failover",
            "default_provider_id": provider_id,
            "manual_allow_fallback": True,
            "allowed_provider_ids": [provider_id],
            "owner_user_id": owner_user_id,
            "balance_amount": 10,
        },
    )
    _assert(response.status_code == 400, f"shared wallet create should fail with 400: {response.status_code} {response.text}")
    _assert("balance_amount" in response.text, f"shared wallet error should mention balance_amount: {response.text}")


def main() -> None:
    with TestClient(app) as client:
        _bootstrap_admin(client)
        shared_user_id = _create_user(
            client,
            username="stage9-user",
            email="stage9-user@example.com",
            password="Stage9User#123",
        )
        stream_disabled_provider = _create_provider(
            client,
            name="stage9-stream-disabled",
            model_name="nonstream-model",
            supports_stream=False,
            supports_vision=False,
            priority=10,
        )
        stream_enabled_provider = _create_provider(
            client,
            name="stage9-stream-enabled",
            model_name="stream-model",
            supports_stream=True,
            supports_vision=False,
            priority=20,
        )
        vision_provider = _create_provider(
            client,
            name="stage9-vision-provider",
            model_name="vision-model",
            supports_stream=True,
            supports_vision=True,
            priority=30,
        )

        _assert_stream_routing_uses_supported_provider(expected_provider_id=stream_enabled_provider["id"])
        _assert_non_stream_model_is_filtered(provider_id=stream_disabled_provider["id"])

        dated_key = _create_api_key(
            client,
            name="stage9-dated-key",
            allowed_provider_ids=[vision_provider["id"]],
            default_provider_id=vision_provider["id"],
            expires_at="2030-01-01T00:00:00Z",
        )
        _assert(dated_key["expires_at"] is not None, f"dated api key should preserve expires_at: {dated_key}")

        runtime_key = _create_api_key(
            client,
            name="stage9-runtime-key",
            allowed_provider_ids=[vision_provider["id"]],
            default_provider_id=vision_provider["id"],
        )
        batched_touch_key = _create_api_key(
            client,
            name="stage9-batched-touch-key",
            allowed_provider_ids=[vision_provider["id"]],
            default_provider_id=vision_provider["id"],
        )

        _assert_shared_wallet_balance_rejected(
            client,
            provider_id=vision_provider["id"],
            owner_user_id=shared_user_id,
        )
        _assert_last_used_touch_is_batched(batched_touch_key["raw_api_key"], batched_touch_key["id"])
        _assert_responses_image_native_passthrough(provider_id=vision_provider["id"])
        _assert_crlf_sse_parsing()
        _assert_chat_stream_to_responses_adapter()
        _assert_stream_probe_payload_uses_true_flag()

        with SessionLocal() as db:
            provider_model = db.scalar(
                select(ProviderModel).where(
                    ProviderModel.provider_id == stream_enabled_provider["id"],
                    ProviderModel.model_name == "stream-model",
                )
            )
            _assert(provider_model is not None and provider_model.supports_stream is True, "stream-enabled provider model missing")

    print("stage9 regression check passed")


if __name__ == "__main__":
    main()
