from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch


TEMP_DB_PATH = Path("data/stage8-log-regression.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./data/stage8-log-regression.db"

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models.api_client_key import ApiClientKey
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.services.log_service import LogService
from app.services.proxy_service import ProxyService


async def _fake_forward_json(provider, endpoint_path: str, payload: dict):
    await asyncio.sleep(0.01)
    session_id = (
        payload.get("metadata", {}).get("session_id")
        if isinstance(payload.get("metadata"), dict)
        else None
    ) or "unknown-session"
    return {
        "id": f"resp-{provider.name}-{session_id}",
        "model": payload.get("model", "unknown"),
        "choices": [
            {
                "message": {"content": f"hello from {provider.name} for {session_id}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 24,
            "completion_tokens": 12,
            "total_tokens": 36,
            "prompt_tokens_details": {
                "cached_tokens": 7,
                "cache_creation_tokens": 3,
            },
        },
    }, f"upstream-{provider.name}-{session_id}"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _bootstrap_admin(client: TestClient) -> None:
    response = client.post(
        "/setup-admin",
        data={
            "username": "stage8-admin",
            "email": "stage8-admin@example.com",
            "password": "Stage8Admin#123",
            "password_confirm": "Stage8Admin#123",
        },
        follow_redirects=False,
    )
    _assert(response.status_code == 303, f"bootstrap admin failed: {response.text}")


def _create_user(client: TestClient, *, username: str, email: str, password: str) -> None:
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


def _create_provider(client: TestClient) -> dict:
    response = client.post(
        "/api/providers",
        json={
            "name": "stage8-provider",
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
                    "model_name": "log-model",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "enabled": True,
                    "input_price_per_1k": 0.003,
                    "output_price_per_1k": 0.006,
                }
            ],
            "remark": "stage8 provider",
        },
    )
    _assert(response.status_code == 201, f"create provider failed: {response.text}")
    return response.json()


def _create_api_key(
    client: TestClient,
    *,
    name: str,
    provider_id: int,
    owner_user_id: int,
) -> dict:
    response = client.post(
        "/api/api-keys",
        json={
            "name": name,
            "remark": f"stage8 {name}",
            "enabled": True,
            "token_limit_total": 5000,
            "route_mode": "failover",
            "default_provider_id": provider_id,
            "manual_allow_fallback": True,
            "allowed_provider_ids": [provider_id],
            "owner_user_id": owner_user_id,
        },
    )
    _assert(response.status_code == 201, f"create api key failed: {response.text}")
    return response.json()


def _login(client: TestClient, *, identifier: str, password: str) -> None:
    response = client.post(
        "/login",
        data={"identifier": identifier, "password": password},
        follow_redirects=False,
    )
    _assert(response.status_code == 303, f"login failed: {response.text}")


def main() -> None:
    with patch.object(ProxyService, "_forward_json", side_effect=_fake_forward_json):
        with TestClient(app) as client:
            _bootstrap_admin(client)
            _create_user(client, username="stage8-user-a", email="stage8-user-a@example.com", password="Stage8User#123")
            _create_user(client, username="stage8-user-b", email="stage8-user-b@example.com", password="Stage8User#123")
            provider = _create_provider(client)

            with SessionLocal() as db:
                user_a = db.scalar(select(UserAccount).where(UserAccount.username == "stage8-user-a"))
                user_b = db.scalar(select(UserAccount).where(UserAccount.username == "stage8-user-b"))
                provider_record = db.get(Provider, provider["id"])
                _assert(user_a is not None and user_b is not None and provider_record is not None, "bootstrap records missing")
                provider_model = next((item for item in provider_record.provider_models if item.model_name == "log-model"), None)
                _assert(provider_model is not None, "provider model missing")
                provider_model.price_multiplier = 1.5
                provider_model.input_price_per_1k = 0.003
                provider_model.output_price_per_1k = 0.006
                user_a.balance_amount = 50
                user_a.total_recharge_amount = 50
                user_b.balance_amount = 50
                user_b.total_recharge_amount = 50
                db.commit()
                user_a_id = user_a.id
                user_b_id = user_b.id

            api_key_a = _create_api_key(client, name="stage8-key-a", provider_id=provider["id"], owner_user_id=user_a_id)
            api_key_b = _create_api_key(client, name="stage8-key-b", provider_id=provider["id"], owner_user_id=user_b_id)

            response_a = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key_a['raw_api_key']}"},
                json={
                    "model": "log-model",
                    "messages": [{"role": "user", "content": "hello a"}],
                    "metadata": {"session_id": "sess-u1"},
                    "reasoning_effort": "medium",
                },
            )
            _assert(response_a.status_code == 200, f"user a request failed: {response_a.text}")

            response_b = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key_b['raw_api_key']}"},
                json={
                    "model": "log-model",
                    "messages": [{"role": "user", "content": "hello b"}],
                    "metadata": {"session_id": "sess-u2"},
                    "reasoning": {"effort": "high"},
                },
            )
            _assert(response_b.status_code == 200, f"user b request failed: {response_b.text}")

            with SessionLocal() as db:
                key_a_record = db.get(ApiClientKey, api_key_a["id"])
                _assert(key_a_record is not None, "api key a missing")
                LogService.create_log(
                    db,
                    log_type="health_check_model",
                    provider_id=provider["id"],
                    provider_name=provider["name"],
                    model_name="log-model",
                    requested_model="log-model",
                    session_id="health-u1",
                    request_path="/internal/health",
                    http_method="GET",
                    success=False,
                    status_code=503,
                    api_client_key_id=key_a_record.id,
                    api_client_key_name=key_a_record.name,
                    api_client_key_prefix=key_a_record.key_prefix,
                    user_account_id=user_a_id,
                    user_account_name="stage8-user-a",
                    message="health probe",
                    trace=[{"result": "health_probe", "latency_ms": 0}],
                    schedule_token_fill=False,
                )

                user_a_success_log = db.scalar(
                    select(RequestLog)
                    .where(RequestLog.api_client_key_id == api_key_a["id"], RequestLog.log_type == "chat", RequestLog.success.is_(True))
                    .order_by(RequestLog.id.desc())
                )
                _assert(user_a_success_log is not None, "user a success log missing")
                _assert(user_a_success_log.session_id == "sess-u1", f"session_id not persisted: {user_a_success_log.session_id}")
                _assert(user_a_success_log.user_account_id == user_a_id, f"user_account_id not persisted: {user_a_success_log.user_account_id}")
                _assert(user_a_success_log.user_account_name == "stage8-user-a", f"user_account_name not persisted: {user_a_success_log.user_account_name}")
                _assert(user_a_success_log.http_method == "POST", f"http_method not persisted: {user_a_success_log.http_method}")
                _assert(user_a_success_log.reasoning_level == "medium", f"reasoning_level not persisted: {user_a_success_log.reasoning_level}")
                _assert(user_a_success_log.attempt_count == 1, f"attempt_count not persisted: {user_a_success_log.attempt_count}")
                _assert(user_a_success_log.ttfb_ms is not None, "ttfb_ms should be persisted")
                _assert(user_a_success_log.duration_ms is not None, "duration_ms should be persisted")
                _assert(user_a_success_log.tps is not None, "tps should be persisted")
                _assert(user_a_success_log.cache_read_tokens == 7, f"cache_read_tokens mismatch: {user_a_success_log.cache_read_tokens}")
                _assert(user_a_success_log.cache_write_tokens == 3, f"cache_write_tokens mismatch: {user_a_success_log.cache_write_tokens}")
                _assert(user_a_success_log.billing_multiplier == 1.5, f"billing_multiplier mismatch: {user_a_success_log.billing_multiplier}")
                _assert(user_a_success_log.channel_price_input_per_1k == 0.003, f"input channel price mismatch: {user_a_success_log.channel_price_input_per_1k}")
                _assert(user_a_success_log.channel_price_output_per_1k == 0.006, f"output channel price mismatch: {user_a_success_log.channel_price_output_per_1k}")

            admin_logs = client.get(f"/api/logs?user_account_id={user_a_id}&exclude_health_checks=false&page=1&page_size=20")
            _assert(admin_logs.status_code == 200, f"admin logs query failed: {admin_logs.text}")
            admin_logs_payload = admin_logs.json()
            _assert(admin_logs_payload["total"] == 2, f"admin user filter total mismatch: {admin_logs_payload}")
            _assert(any(item["session_id"] == "sess-u1" for item in admin_logs_payload["items"]), f"user a session missing: {admin_logs_payload}")
            _assert(any(item["log_type"] == "health_check_model" for item in admin_logs_payload["items"]), f"health log missing from admin view: {admin_logs_payload}")

            admin_logs_non_health = client.get(f"/api/logs?user_account_id={user_a_id}&exclude_health_checks=true&page=1&page_size=20")
            _assert(admin_logs_non_health.status_code == 200, f"admin non-health logs query failed: {admin_logs_non_health.text}")
            admin_logs_non_health_payload = admin_logs_non_health.json()
            _assert(admin_logs_non_health_payload["total"] == 1, f"health logs should be excluded: {admin_logs_non_health_payload}")
            filtered_item = admin_logs_non_health_payload["items"][0]
            _assert(filtered_item["reasoning_level"] == "medium", f"reasoning_level api mismatch: {filtered_item}")
            _assert(filtered_item["cache_read_tokens"] == 7 and filtered_item["cache_write_tokens"] == 3, f"cache token api mismatch: {filtered_item}")
            _assert(filtered_item["billing_multiplier"] == 1.5, f"multiplier api mismatch: {filtered_item}")

            filter_options_response = client.get("/api/logs/filter-options?exclude_health_checks=true")
            _assert(filter_options_response.status_code == 200, f"filter options failed: {filter_options_response.text}")
            filter_options = filter_options_response.json()
            _assert(any(item["value"] == str(user_a_id) for item in filter_options["users"]), f"user a filter option missing: {filter_options}")
            _assert(any(item["value"] == str(user_b_id) for item in filter_options["users"]), f"user b filter option missing: {filter_options}")

            admin_logs_page = client.get("/logs")
            _assert(admin_logs_page.status_code == 200, f"admin logs page failed: {admin_logs_page.text}")
            _assert("日志中心" in admin_logs_page.text and "关键列日志列表" in admin_logs_page.text, "admin logs page headers missing")

            client.get("/logout")
            _login(client, identifier="stage8-user-a", password="Stage8User#123")
            user_logs_page = client.get("/user/logs")
            _assert(user_logs_page.status_code == 200, f"user logs page failed: {user_logs_page.text}")
            _assert("sess-u1" in user_logs_page.text, "user own session missing from user logs page")
            _assert("sess-u2" not in user_logs_page.text, "other user session leaked into user logs page")
            _assert("health-u1" not in user_logs_page.text, "health check log leaked into user logs page")
            _assert("请求记录与排障入口" in user_logs_page.text and "关键列请求记录" in user_logs_page.text, "user logs page headers missing")

    print("stage8 log regression passed")


if __name__ == "__main__":
    main()
