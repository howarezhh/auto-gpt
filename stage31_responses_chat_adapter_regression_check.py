from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models.responses_chat_adapter_session import ResponsesChatAdapterSession
from app.models.provider import Provider
from app.services.api_key_service import require_api_client_auth
from app.services.proxy_service import ProxyService
from app.services.redis_service import RedisService
from app.services.responses_chat_adapter_service import (
    ADAPTER_MARKER_KEY,
    ADAPTER_RESPONSE_ID_KEY,
    ADAPTER_RESPONSE_MODEL_KEY,
    AdapterConversationState,
    ResponsesChatAdapterService,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _provider() -> Provider:
    return Provider(
        id=1,
        name="适配测试中转站",
        provider_type="openai_compatible",
        base_url="https://example.com/v1",
        api_key="sk-test",
    )


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def setex(self, key: str, _ttl: int, value: str):
        self.values[key] = value
        return True

    async def set(self, key: str, value: str):
        self.values[key] = value
        return True


def _fake_auth_context():
    return SimpleNamespace(
        route_context=None,
        api_client_key=SimpleNamespace(
            id=1,
            name="适配测试密钥",
            key_prefix="sk-aotu-test",
            owner_user_id=None,
            owner_user=None,
            tenant_name=None,
            project_name=None,
            app_name=None,
            environment_name=None,
        ),
        remaining_tokens=None,
        remaining_balance=None,
        remaining_requests_daily=None,
        remaining_cost_daily=None,
        policy_snapshot_json="{}",
    )


async def _prepare_and_state_roundtrip_should_preserve_prefix() -> None:
    settings = get_settings()
    settings.responses_chat_adapter_storage_type = "memory"
    settings.responses_chat_adapter_model_map_json = '{"gpt-4o":"deepseek-chat"}'
    settings.responses_chat_adapter_max_tool_rounds = 10
    settings.responses_chat_adapter_context_window_tokens = 128000
    ResponsesChatAdapterService._memory_sessions.clear()

    prepared = await ResponsesChatAdapterService.prepare_request(
        {
            "model": "gpt-4o",
            "instructions": "你是稳定系统提示。",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "看图"},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
                    ],
                }
            ],
        }
    )
    _assert(prepared.upstream_model == "deepseek-chat", f"model map failed: {prepared}")
    _assert(prepared.chat_payload["messages"][0] == {"role": "system", "content": "你是稳定系统提示。"}, "instructions should be first system message")
    image_part = prepared.chat_payload["messages"][1]["content"][1]
    _assert(image_part["type"] == "image_url", f"image input not converted: {image_part}")
    _assert(prepared.chat_payload[ADAPTER_MARKER_KEY] is True, "stream marker should exist in prepared payload")
    _assert(prepared.chat_payload[ADAPTER_RESPONSE_ID_KEY] == prepared.response_id, "stream response id override should be prepared response id")

    chat_response = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_b", "type": "function", "function": {"name": "b", "arguments": "{}"}},
                        {"id": "call_a", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    response_payload = ProxyService._convert_chat_completion_to_responses_payload(chat_response, requested_model="gpt-4o")
    response_payload["id"] = prepared.response_id
    await ResponsesChatAdapterService.persist_response(
        prepared=prepared,
        responses_payload=response_payload,
        chat_response=chat_response,
    )

    follow_up = await ResponsesChatAdapterService.prepare_request(
        {
            "model": "gpt-4o",
            "previous_response_id": prepared.response_id,
            "instructions": "后续不能重复插入。",
            "input": [
                {"type": "function_call_output", "call_id": "call_a", "output": "A"},
                {"type": "function_call_output", "call_id": "call_b", "output": "B"},
            ],
        }
    )
    messages = follow_up.chat_payload["messages"]
    _assert([item for item in messages if item.get("role") == "system"] == [{"role": "system", "content": "你是稳定系统提示。"}], f"system prefix changed: {messages}")
    tool_messages = [item for item in messages if item.get("role") == "tool"]
    _assert([item["tool_call_id"] for item in tool_messages] == ["call_b", "call_a"], f"tool output order should follow assistant tool_calls: {tool_messages}")
    trace_item = ResponsesChatAdapterService._trace_item(follow_up, stream=False)
    _assert(trace_item["history_cache_hit"] is True, f"previous_response_id load should be observable: {trace_item}")
    _assert(trace_item["cache_prefix_policy"] == "immutable_history_prefix", f"cache policy missing: {trace_item}")


def _builtin_tools_should_fail_closed() -> None:
    get_settings().responses_chat_adapter_web_search_enabled = False
    for tool_type in ("web_search", "file_search", "code_interpreter"):
        try:
            ResponsesChatAdapterService._reject_unsupported_builtin_tools({"tools": [{"type": tool_type}]})
        except HTTPException as exc:
            _assert(exc.status_code == 400, f"{tool_type} should fail with 400")
            continue
        raise AssertionError(f"{tool_type} should be rejected")


async def _web_search_enabled_requires_proxy_url() -> None:
    settings = get_settings()
    settings.responses_chat_adapter_web_search_enabled = True
    settings.responses_chat_adapter_search_proxy_url = ""
    try:
        await ResponsesChatAdapterService.prepare_request(
            {
                "model": "gpt-4o",
                "input": "查一下今天新闻",
                "tools": [{"type": "web_search"}],
            }
        )
    except HTTPException as exc:
        _assert(exc.status_code == 400, "web_search without proxy should fail before upstream")
        _assert(exc.detail["code"] == "responses_chat_adapter_web_search_proxy_not_configured", f"unexpected error: {exc.detail}")
        return
    finally:
        settings.responses_chat_adapter_web_search_enabled = False
        settings.responses_chat_adapter_search_proxy_url = ""
    raise AssertionError("enabled web_search without proxy URL should be rejected")


def _upstream_marker_should_be_stripped() -> None:
    prepared = ProxyService._prepare_upstream_request(
        _provider(),
        endpoint_path="/chat/completions",
        payload={
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": "hi"}],
            ADAPTER_MARKER_KEY: True,
            ADAPTER_RESPONSE_ID_KEY: "resp_adapter_stream",
            ADAPTER_RESPONSE_MODEL_KEY: "gpt-4o",
        },
    )
    _assert(prepared.adapt_chat_response_to_responses is True, "adapter marker should enable response adapter")
    _assert(prepared.response_model_override == "gpt-4o", "response model override missing")
    _assert(prepared.response_id_override == "resp_adapter_stream", "response id override missing")
    _assert(ADAPTER_MARKER_KEY not in prepared.request_payload, "internal marker leaked upstream")
    _assert(ADAPTER_RESPONSE_MODEL_KEY not in prepared.request_payload, "response model override leaked upstream")
    _assert(ADAPTER_RESPONSE_ID_KEY not in prepared.request_payload, "response id override leaked upstream")


def _chat_stream_should_emit_responses_events() -> None:
    started = time.perf_counter()
    state = ProxyService._create_responses_stream_state(payload={"model": "deepseek-chat"}, response_id="resp_adapter_stream")
    chunk = (
        b'data: {"id":"chatcmpl-1","created":1,"model":"deepseek-chat","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":"{}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    )
    events = b"".join(
        ProxyService._adapt_chat_stream_chunk_to_responses_events(
            chunk,
            state=state,
            requested_model="gpt-4o",
        )
    ).decode("utf-8")
    _assert("response.created" in events, f"missing response.created: {events}")
    _assert("response.output_text.delta" in events, f"missing text delta: {events}")
    _assert("response.function_call.completed" in events, f"missing function call completion: {events}")
    _assert("resp_adapter_stream" in events, f"adapter response id should be used for stream events: {events}")
    _assert("resp_" not in events.replace("resp_adapter_stream", ""), f"stream should not mix multiple response ids: {events}")
    _assert('"model":"gpt-4o"' in events or '"model": "gpt-4o"' in events, f"requested model not preserved: {events}")
    _assert("chatcmpl-1" not in events, f"chat id should not become response id: {events}")
    elapsed_ms = (time.perf_counter() - started) * 1000
    _assert(elapsed_ms < 20, f"stream conversion overhead should stay below 20ms in local conversion test: {elapsed_ms:.3f}ms")


async def _history_should_compact_once_at_context_threshold() -> None:
    settings = get_settings()
    settings.responses_chat_adapter_context_window_tokens = 40
    prepared = await ResponsesChatAdapterService.prepare_request(
        {
            "model": "gpt-4o",
            "instructions": "系统提示",
            "input": [
                {"role": "user", "content": f"第 {index} 轮 " + "很长的上下文" * 10}
                for index in range(8)
            ],
        }
    )
    messages = prepared.chat_payload["messages"]
    _assert(messages[0]["role"] == "system", f"compacted history should start with system: {messages}")
    _assert("[Responses→Chat adapter immutable summary]" in messages[0]["content"], f"summary marker missing: {messages[0]}")
    compacted_again = ResponsesChatAdapterService._maybe_compact_history_once(messages + [{"role": "user", "content": "追加"}])
    _assert(compacted_again[0]["content"] == messages[0]["content"], "summary system should not be rewritten repeatedly")
    settings.responses_chat_adapter_context_window_tokens = 128000


async def _storage_backends_should_roundtrip() -> None:
    settings = get_settings()
    state = AdapterConversationState(
        response_id="resp_storage_test",
        requested_model="gpt-4o",
        upstream_model="deepseek-chat",
        instructions="系统",
        messages=[{"role": "system", "content": "系统"}, {"role": "user", "content": "你好"}],
        pending_tool_call_ids=["call_1"],
        tool_round_count=1,
    )

    settings.responses_chat_adapter_storage_type = "redis"
    fake_redis = _FakeRedis()
    with patch.object(RedisService, "get_client", return_value=fake_redis):
        await ResponsesChatAdapterService.save_state(state, previous_response_id=None)
        loaded = await ResponsesChatAdapterService.load_state("resp_storage_test")
    _assert(loaded is not None and loaded.pending_tool_call_ids == ["call_1"], f"redis storage roundtrip failed: {loaded}")

    settings.responses_chat_adapter_storage_type = "database"
    Base.metadata.tables["responses_chat_adapter_sessions"].create(bind=engine, checkfirst=True)
    db = SessionLocal()
    try:
        existing = db.get(ResponsesChatAdapterSession, "resp_storage_test")
        if existing is not None:
            db.delete(existing)
            db.commit()
    finally:
        db.close()
    await ResponsesChatAdapterService.save_state(state, previous_response_id=None)
    loaded = await ResponsesChatAdapterService.load_state("resp_storage_test")
    _assert(loaded is not None and loaded.messages[-1]["content"] == "你好", f"database storage roundtrip failed: {loaded}")
    db = SessionLocal()
    try:
        row = db.get(ResponsesChatAdapterSession, "resp_storage_test")
        if row is not None:
            db.delete(row)
            db.commit()
    finally:
        db.close()
    settings.responses_chat_adapter_storage_type = "memory"


def _http_responses_endpoint_should_switch_by_flag() -> None:
    settings = get_settings()
    app.dependency_overrides[require_api_client_auth] = _fake_auth_context

    class _Lease:
        pass

    async def fake_acquire(**_kwargs):
        return _Lease()

    async def fake_release(_lease):
        return None

    async def fake_adapter_forward_json_response(**_kwargs):
        return (
            {"id": "resp_adapter", "object": "response", "status": "completed", "output": [], "usage": {}},
            _provider(),
            [{"result": "adapter"}],
            3,
        )

    async def fake_native_forward_json_request(**_kwargs):
        return (
            {"id": "resp_native", "object": "response", "status": "completed", "output": [], "usage": {}},
            _provider(),
            [{"result": "native"}],
            2,
        )

    try:
        with (
            patch("app.routers.proxy._acquire_request_concurrency", side_effect=fake_acquire),
            patch("app.routers.proxy._release_request_concurrency", side_effect=fake_release),
            patch.object(ResponsesChatAdapterService, "forward_json_response", side_effect=fake_adapter_forward_json_response),
            patch.object(ProxyService, "forward_json_request", side_effect=fake_native_forward_json_request),
        ):
            client = TestClient(app)
            settings.responses_chat_adapter_enabled = True
            adapter_response = client.post("/v1/responses", json={"model": "gpt-4o", "input": "你好"})
            _assert(adapter_response.status_code == 200, adapter_response.text)
            _assert(adapter_response.json()["id"] == "resp_adapter", f"enabled flag should use adapter: {adapter_response.json()}")

            settings.responses_chat_adapter_enabled = False
            native_response = client.post("/v1/responses", json={"model": "gpt-4o", "input": "你好"})
            _assert(native_response.status_code == 200, native_response.text)
            _assert(native_response.json()["id"] == "resp_native", f"disabled flag should use native path: {native_response.json()}")
    finally:
        app.dependency_overrides.pop(require_api_client_auth, None)
        settings.responses_chat_adapter_enabled = False


def _http_responses_endpoint_should_run_real_adapter_json_path() -> None:
    settings = get_settings()
    app.dependency_overrides[require_api_client_auth] = _fake_auth_context
    settings.responses_chat_adapter_enabled = True
    settings.responses_chat_adapter_storage_type = "memory"
    settings.responses_chat_adapter_model_map_json = '{"gpt-4o":"deepseek-chat"}'
    ResponsesChatAdapterService._memory_sessions.clear()

    class _Lease:
        pass

    async def fake_acquire(**_kwargs):
        return _Lease()

    async def fake_release(_lease):
        return None

    async def fake_forward_json_request(**kwargs):
        _assert(kwargs["endpoint_path"] == "/chat/completions", f"adapter should call chat endpoint: {kwargs}")
        payload = kwargs["payload"]
        _assert(payload["model"] == "deepseek-chat", f"mapped upstream model missing: {payload}")
        _assert(payload["messages"][0] == {"role": "system", "content": "固定系统"}, f"instructions not converted: {payload}")
        _assert(payload["messages"][1] == {"role": "user", "content": "你好"}, f"input not converted: {payload}")
        _assert(ADAPTER_MARKER_KEY not in payload, f"internal marker leaked to upstream payload: {payload}")
        _assert(ADAPTER_RESPONSE_MODEL_KEY not in payload, f"model override leaked to upstream payload: {payload}")
        _assert(ADAPTER_RESPONSE_ID_KEY not in payload, f"response id override leaked to upstream payload: {payload}")
        trace = kwargs.get("route_retry_trace") or []
        _assert(trace and trace[0]["result"] == "responses_chat_adapter", f"adapter trace missing: {trace}")
        return (
            {
                "id": "chatcmpl-json",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "适配成功"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
            _provider(),
            [{"result": "fake_upstream"}],
            4,
        )

    try:
        with (
            patch("app.routers.proxy._acquire_request_concurrency", side_effect=fake_acquire),
            patch("app.routers.proxy._release_request_concurrency", side_effect=fake_release),
            patch.object(ProxyService, "forward_json_request", side_effect=fake_forward_json_request),
        ):
            client = TestClient(app)
            response = client.post(
                "/v1/responses",
                json={"model": "gpt-4o", "instructions": "固定系统", "input": "你好"},
            )
            _assert(response.status_code == 200, response.text)
            body = response.json()
            _assert(body["object"] == "response", f"response object mismatch: {body}")
            _assert(body["model"] == "gpt-4o", f"requested model should be preserved: {body}")
            _assert(body.get("output_text") == "适配成功", f"chat response not converted: {body}")
            loaded = asyncio.run(ResponsesChatAdapterService.load_state(body["id"]))
            _assert(loaded is not None, "adapter response state should be persisted")
            _assert(loaded.messages[-1] == {"role": "assistant", "content": "适配成功"}, f"assistant history not persisted: {loaded}")
    finally:
        app.dependency_overrides.pop(require_api_client_auth, None)
        settings.responses_chat_adapter_enabled = False


async def _stream_wrapper_should_emit_failed_and_done_without_reraising() -> None:
    settings = get_settings()
    settings.responses_chat_adapter_storage_type = "memory"
    settings.responses_chat_adapter_model_map_json = '{"gpt-4o":"deepseek-chat"}'

    async def broken_stream():
        yield b"data: {}\n\n"
        raise RuntimeError("upstream stream exploded")

    async def fake_forward_stream_request(**_kwargs):
        return broken_stream(), _provider(), [{"result": "fake_stream"}], 1

    with patch.object(ProxyService, "forward_stream_request", side_effect=fake_forward_stream_request):
        stream, _provider_obj, _trace, _latency = await ResponsesChatAdapterService.forward_stream_response(
            payload={"model": "gpt-4o", "input": "你好", "stream": True}
        )
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
    text = b"".join(chunks).decode("utf-8")
    _assert("response.failed" in text, f"stream failure event missing: {text}")
    _assert("data: [DONE]" in text, f"stream failure should terminate with DONE: {text}")


async def _prepare_latency_should_stay_under_budget() -> None:
    settings = get_settings()
    settings.responses_chat_adapter_context_window_tokens = 128000
    started = time.perf_counter()
    await ResponsesChatAdapterService.prepare_request(
        {
            "model": "gpt-4o",
            "instructions": "系统提示",
            "input": "你好",
        }
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    _assert(elapsed_ms < 50, f"non-stream conversion overhead should stay below 50ms in local prepare test: {elapsed_ms:.3f}ms")


def _env_upstream_specs_should_drive_model_mapping() -> None:
    settings = get_settings()
    settings.responses_chat_adapter_upstreams_json = (
        '{"gpt-4o":{"upstream_model":"deepseek-chat","base_url":"https://api.deepseek.example/v1","api_key":"sk-deepseek"},'
        '"gpt-4o-mini":{"upstream_model":"qwen-plus","base_url":"https://dashscope.example/compatible-mode/v1","api_key":"sk-qwen"}}'
    )
    settings.responses_chat_adapter_model_map_json = '{"gpt-4o":"fallback-model"}'
    specs = ResponsesChatAdapterService._env_upstream_specs()
    _assert(len(specs) == 2, f"expected two env upstream specs: {specs}")
    _assert(ResponsesChatAdapterService._mapped_model("gpt-4o") == "deepseek-chat", f"env upstream should override simple model map: {specs}")
    settings.responses_chat_adapter_upstreams_json = ""
    settings.responses_chat_adapter_model_map_json = '{"gpt-4o":"deepseek-chat"}'


def main() -> None:
    asyncio.run(_prepare_and_state_roundtrip_should_preserve_prefix())
    _builtin_tools_should_fail_closed()
    asyncio.run(_web_search_enabled_requires_proxy_url())
    _upstream_marker_should_be_stripped()
    _chat_stream_should_emit_responses_events()
    asyncio.run(_history_should_compact_once_at_context_threshold())
    asyncio.run(_storage_backends_should_roundtrip())
    _http_responses_endpoint_should_switch_by_flag()
    _http_responses_endpoint_should_run_real_adapter_json_path()
    asyncio.run(_stream_wrapper_should_emit_failed_and_done_without_reraising())
    asyncio.run(_prepare_latency_should_stay_under_budget())
    _env_upstream_specs_should_drive_model_mapping()
    print("stage31 responses chat adapter regression check passed")


if __name__ == "__main__":
    main()
