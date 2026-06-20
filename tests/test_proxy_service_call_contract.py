from __future__ import annotations

import ast
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.services.proxy_service import ProxyService


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROXY_SERVICE_PATH = PROJECT_ROOT / "app" / "services" / "proxy_service.py"


def _keyword_names(call: ast.Call) -> set[str]:
    return {keyword.arg for keyword in call.keywords if keyword.arg is not None}


def test_mapped_model_retry_calls_keep_required_protocol_argument() -> None:
    tree = ast.parse(PROXY_SERVICE_PATH.read_text(encoding="utf-8"))
    required_methods = {
        "_retry_with_next_mapped_model_json",
        "_retry_with_next_mapped_model_stream",
    }
    checked_calls = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not isinstance(function, ast.Attribute) or function.attr not in required_methods:
            continue

        checked_calls += 1
        assert "required_upstream_protocol_type" in _keyword_names(node), function.attr

    assert checked_calls >= len(required_methods)


def test_openai_compatible_upstream_request_uses_upstream_model_id_and_preserves_public_model_id() -> None:
    provider = SimpleNamespace(base_url="https://provider.example.com/v1", protocol_type="openai_compatible")
    provider_model = SimpleNamespace(
        model_name="platform-model-id",
        upstream_model_name="upstream-model-id",
        protocol_type="chat_completions",
        model_group="openai",
    )

    prepared = ProxyService._prepare_upstream_request(
        provider,
        endpoint_path="/chat/completions",
        payload={"model": "platform-model-id", "messages": [{"role": "user", "content": "hi"}]},
        provider_model=provider_model,
    )

    assert prepared.request_payload["model"] == "upstream-model-id"
    assert prepared.response_model_override == "platform-model-id"


def test_openai_compatible_upstream_request_falls_back_to_platform_model_id_without_upstream_id() -> None:
    provider = SimpleNamespace(base_url="https://provider.example.com/v1", protocol_type="openai_compatible")
    provider_model = SimpleNamespace(
        model_name="platform-model-id",
        upstream_model_name=None,
        protocol_type="chat_completions",
        model_group="openai",
    )

    prepared = ProxyService._prepare_upstream_request(
        provider,
        endpoint_path="/chat/completions",
        payload={"model": "platform-model-id", "messages": [{"role": "user", "content": "hi"}]},
        provider_model=provider_model,
    )

    assert prepared.request_payload["model"] == "platform-model-id"
    assert prepared.response_model_override == "platform-model-id"


class _FakeSessionContext:
    def __init__(self, session: object) -> None:
        self.session = session
        self.entered = False
        self.exited = False

    def __enter__(self) -> object:
        self.entered = True
        return self.session

    def __exit__(self, exc_type, exc, tb) -> None:
        self.exited = True


@pytest.mark.anyio
async def test_public_forward_json_request_opens_scoped_session_when_db_is_missing(monkeypatch) -> None:
    fake_db = object()
    context = _FakeSessionContext(fake_db)
    seen: dict[str, object] = {}

    async def fake_once(db, **kwargs):
        seen["db"] = db
        seen["kwargs"] = kwargs
        return {"ok": True}, SimpleNamespace(id=1, name="测试提供商"), [], 1

    monkeypatch.setattr("app.services.proxy_service.SessionLocal", lambda: context)
    monkeypatch.setattr(ProxyService, "_forward_json_request_once", staticmethod(fake_once))

    result, provider, trace, latency_ms = await ProxyService.forward_json_request(
        endpoint_path="/chat/completions",
        payload={"model": "测试模型"},
        log_type="chat",
    )

    assert result == {"ok": True}
    assert provider.name == "测试提供商"
    assert trace == []
    assert latency_ms == 1
    assert seen["db"] is fake_db
    assert context.entered is True
    assert context.exited is True


@pytest.mark.anyio
async def test_public_forward_stream_request_opens_scoped_session_when_db_is_missing(monkeypatch) -> None:
    fake_db = object()
    context = _FakeSessionContext(fake_db)
    seen: dict[str, object] = {}

    async def fake_stream():
        yield b"data: {}\n\n"

    async def fake_once(db, **kwargs):
        seen["db"] = db
        seen["kwargs"] = kwargs
        return fake_stream(), SimpleNamespace(id=1, name="测试提供商"), [], 1

    monkeypatch.setattr("app.services.proxy_service.SessionLocal", lambda: context)
    monkeypatch.setattr(ProxyService, "_forward_stream_request_once", staticmethod(fake_once))

    stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
        endpoint_path="/chat/completions",
        payload={"model": "测试模型", "stream": True},
        log_type="chat",
    )

    assert [chunk async for chunk in stream] == [b"data: {}\n\n"]
    assert provider.name == "测试提供商"
    assert trace == []
    assert latency_ms == 1
    assert seen["db"] is fake_db
    assert context.entered is True
    assert context.exited is True
