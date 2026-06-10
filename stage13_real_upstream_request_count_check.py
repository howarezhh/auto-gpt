from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


TEMP_DB_PATH = Path("data/stage13-real-upstream-count.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test",
)
os.environ["ENABLE_SCHEDULER"] = "false"

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app
from app.models.request_log import RequestLog
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


class _Recorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[tuple[str, str | None]] = []

    def reset(self) -> None:
        with self._lock:
            self._calls = []

    def record(self, path: str, model: str | None) -> None:
        with self._lock:
            self._calls.append((path, model))

    def calls_for_model(self, model: str) -> list[str]:
        with self._lock:
            return [path for path, current_model in self._calls if current_model == model]


RECORDER = _Recorder()


class _FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return None

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            payload = {}

        model = payload.get("model") if isinstance(payload, dict) else None
        if not isinstance(model, str):
            model = None
        RECORDER.record(self.path, model)

        if self.path == "/v1/chat/completions":
            self._handle_chat(model)
            return
        if self.path == "/v1/responses":
            self._handle_responses(model)
            return
        self._send_json(404, {"error": {"message": f"unknown path {self.path}", "code": "unknown_path"}})

    def _handle_chat(self, model: str | None) -> None:
        if model in {"real-count-chat", "real-count-fallback", "real-count-unsafe"}:
            self._send_json(
                200,
                {
                    "id": f"chat-{model}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": f"chat ok for {model}"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            )
            return
        if model == "real-count-chat-fallback":
            self._send_json(
                404,
                {
                    "error": {
                        "message": f"chat/completions not supported for model {model}",
                        "type": "invalid_request_error",
                        "code": "unsupported_endpoint",
                    }
                },
            )
            return
        self._send_json(400, {"error": {"message": f"unexpected chat model {model}", "code": "unexpected_model"}})

    def _handle_responses(self, model: str | None) -> None:
        if model in {"real-count-resp", "real-count-chat-fallback"}:
            self._send_json(
                200,
                {
                    "id": f"resp-{model}",
                    "object": "response",
                    "created": int(time.time()),
                    "model": model,
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": f"response ok for {model}"}],
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                },
            )
            return
        if model in {"real-count-fallback", "real-count-unsafe"}:
            self._send_json(
                404,
                {
                    "error": {
                        "message": f"responses endpoint not supported for model {model}",
                        "type": "invalid_request_error",
                        "code": "unsupported_endpoint",
                    }
                },
            )
            return
        self._send_json(400, {"error": {"message": f"unexpected responses model {model}", "code": "unexpected_model"}})

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
        if UserAuthService.get_user_by_login(db, "stage13-admin") is None:
            UserAuthService.create_user(
                db,
                username="stage13-admin",
                email="stage13-admin@example.com",
                password="Stage13Admin#123",
                role=USER_ROLE_ADMIN,
                enabled=True,
            )
    _login(client, identifier="stage13-admin", password="Stage13Admin#123")


def _create_provider(client: TestClient, *, upstream_base_url: str) -> dict:
    response = client.post(
        "/api/providers",
        json={
            "name": "stage13-provider",
            "base_url": upstream_base_url,
            "api_key": "upstream-secret",
            "provider_type": "openai_compatible",
            "enabled": True,
            "priority": 10,
            "weight": 100,
            "timeout_ms": 30000,
            "max_retries": 1,
            "model_configs": [
                {
                    "model_name": "real-count-chat",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": True,
                    "enabled": True,
                },
                {
                    "model_name": "real-count-resp",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": True,
                    "enabled": True,
                },
                {
                    "model_name": "real-count-fallback",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": True,
                    "enabled": True,
                },
                {
                    "model_name": "real-count-chat-fallback",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": False,
                    "supports_chat_completions": True,
                    "supports_responses": True,
                    "enabled": True,
                },
                {
                    "model_name": "real-count-unsafe",
                    "priority": 100,
                    "weight": 100,
                    "supports_stream": True,
                    "supports_vision": False,
                    "supports_tools": True,
                    "supports_chat_completions": True,
                    "supports_responses": True,
                    "enabled": True,
                },
            ],
            "remark": "stage13 real upstream counter",
        },
    )
    _assert(response.status_code == 201, f"create provider failed: {response.text}")
    return response.json()


def _create_api_key(client: TestClient, *, provider_id: int) -> dict:
    response = client.post(
        "/api/api-keys",
        json={
            "name": "stage13-key",
            "remark": "stage13 real upstream count key",
            "enabled": True,
            "token_limit_total": 5000,
            "route_mode": "failover",
            "default_provider_id": provider_id,
            "manual_allow_fallback": True,
            "allowed_provider_ids": [provider_id],
        },
    )
    _assert(response.status_code == 201, f"create api key failed: {response.text}")
    return response.json()


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._worker_thread = thread  # type: ignore[attr-defined]
    return server


def main() -> None:
    server = _start_server()
    upstream_base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        with TestClient(app) as client:
            _bootstrap_admin(client)
            provider = _create_provider(client, upstream_base_url=upstream_base_url)
            api_key = _create_api_key(client, provider_id=provider["id"])
            auth_headers = {"Authorization": f"Bearer {api_key['raw_api_key']}"}

            RECORDER.reset()
            chat_response = client.post(
                "/v1/chat/completions",
                headers=auth_headers,
                json={"model": "real-count-chat", "messages": [{"role": "user", "content": "hello"}]},
            )
            _assert(chat_response.status_code == 200, f"chat request failed: {chat_response.text}")
            _assert(
                RECORDER.calls_for_model("real-count-chat") == ["/v1/chat/completions"],
                f"chat request should hit upstream once: {RECORDER.calls_for_model('real-count-chat')}",
            )

            RECORDER.reset()
            responses_response = client.post(
                "/v1/responses",
                headers=auth_headers,
                json={"model": "real-count-resp", "input": "hello"},
            )
            _assert(responses_response.status_code == 200, f"responses request failed: {responses_response.text}")
            _assert(
                RECORDER.calls_for_model("real-count-resp") == ["/v1/responses"],
                f"responses request should hit upstream once: {RECORDER.calls_for_model('real-count-resp')}",
            )

            RECORDER.reset()
            fallback_response = client.post(
                "/v1/responses",
                headers=auth_headers,
                json={"model": "real-count-fallback", "input": "fallback please"},
            )
            _assert(fallback_response.status_code == 200, f"responses fallback failed: {fallback_response.text}")
            _assert(
                RECORDER.calls_for_model("real-count-fallback") == ["/v1/responses", "/v1/chat/completions"],
                f"responses fallback should make exactly two upstream calls: {RECORDER.calls_for_model('real-count-fallback')}",
            )

            RECORDER.reset()
            reverse_fallback_response = client.post(
                "/v1/chat/completions",
                headers=auth_headers,
                json={"model": "real-count-chat-fallback", "messages": [{"role": "user", "content": "fallback back"}]},
            )
            _assert(reverse_fallback_response.status_code == 200, f"chat fallback failed: {reverse_fallback_response.text}")
            _assert(
                RECORDER.calls_for_model("real-count-chat-fallback") == ["/v1/chat/completions", "/v1/responses"],
                (
                    "chat fallback should make exactly two upstream calls: "
                    f"{RECORDER.calls_for_model('real-count-chat-fallback')}"
                ),
            )

            RECORDER.reset()
            unsafe_response = client.post(
                "/v1/responses",
                headers=auth_headers,
                json={
                    "model": "real-count-unsafe",
                    "input": "unsafe fallback",
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "noop",
                                "description": "noop",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                },
            )
            _assert(unsafe_response.status_code >= 400, f"unsafe fallback should be rejected: {unsafe_response.text}")
            _assert(
                RECORDER.calls_for_model("real-count-unsafe") == ["/v1/responses"],
                f"unsafe fallback should not issue a second upstream call: {RECORDER.calls_for_model('real-count-unsafe')}",
            )
            _assert(
                "endpoint_fallback_conversion_unsafe" in unsafe_response.text,
                f"unsafe fallback should surface conversion error: {unsafe_response.text}",
            )

            with SessionLocal() as db:
                log_count = db.scalar(
                    select(func.count()).select_from(RequestLog).where(RequestLog.api_client_key_id == api_key["id"])
                )
                success_count = db.scalar(
                    select(func.count()).select_from(RequestLog).where(
                        RequestLog.api_client_key_id == api_key["id"],
                        RequestLog.success.is_(True),
                    )
                )
            _assert(log_count == 5, f"expected 5 external request logs, got {log_count}")
            _assert(success_count == 4, f"expected 4 successful external request logs, got {success_count}")
    finally:
        server.shutdown()
        server.server_close()
        server._worker_thread.join(timeout=5)  # type: ignore[attr-defined]

    print("stage13 real upstream request count regression passed")


if __name__ == "__main__":
    main()
