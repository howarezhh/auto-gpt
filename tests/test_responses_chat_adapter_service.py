import asyncio

import pytest
from fastapi import HTTPException

from app.services.responses_chat_adapter_service import (
    ADAPTER_MARKER_KEY,
    ADAPTER_RESPONSE_ID_KEY,
    ADAPTER_RESPONSE_MODEL_KEY,
    ResponsesChatAdapterService,
)


def _patch_adapter_settings(monkeypatch, *, context_window_tokens=0):
    values = {
        "responses_chat_adapter_storage_type": "memory",
        "responses_chat_adapter_ttl_seconds": 0,
        "responses_chat_adapter_snapshot_max_bytes": 1048576,
        "responses_chat_adapter_context_window_tokens": context_window_tokens,
        "responses_chat_adapter_max_tool_rounds": 10,
        "responses_chat_adapter_model_map_json": "",
        "responses_chat_adapter_web_search_enabled": False,
    }

    def fake_setting_value(name, default=None):
        return values.get(name, default)

    monkeypatch.setattr(ResponsesChatAdapterService, "_setting_value", staticmethod(fake_setting_value))
    monkeypatch.setattr(ResponsesChatAdapterService, "_env_upstream_specs", staticmethod(lambda: []))
    ResponsesChatAdapterService._memory_sessions.clear()


def _store_memory_state(response_id, *, instructions=None, messages=None, pending=None, tool_round_count=0):
    ResponsesChatAdapterService._memory_sessions[response_id] = (
        {
            "response_id": response_id,
            "requested_model": "gpt-4o",
            "upstream_model": "gpt-4o",
            "instructions": instructions,
            "messages": messages or [],
            "pending_tool_call_ids": pending or [],
            "tool_round_count": tool_round_count,
        },
        None,
    )


def test_prepare_stream_request_includes_usage_and_keeps_adapter_markers(monkeypatch):
    _patch_adapter_settings(monkeypatch)

    prepared = asyncio.run(
        ResponsesChatAdapterService.prepare_request(
            {
                "model": "gpt-4o",
                "input": "ping",
                "stream": True,
                "stream_options": {"existing": "value"},
            }
        )
    )

    assert prepared.chat_payload[ADAPTER_MARKER_KEY] is True
    assert prepared.chat_payload[ADAPTER_RESPONSE_MODEL_KEY] == "gpt-4o"
    assert prepared.chat_payload[ADAPTER_RESPONSE_ID_KEY] == prepared.response_id
    assert prepared.chat_payload["stream_options"] == {"existing": "value", "include_usage": True}


def test_previous_response_without_input_preserves_history_prefix(monkeypatch):
    _patch_adapter_settings(monkeypatch)
    history = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
    ]
    _store_memory_state("resp_prev", instructions="stable system", messages=history)

    prepared = asyncio.run(
        ResponsesChatAdapterService.prepare_request(
            {
                "model": "gpt-4o",
                "previous_response_id": "resp_prev",
            }
        )
    )

    assert prepared.messages_before_response == history
    assert prepared.compacted_history is False


def test_previous_response_rejects_instruction_changes(monkeypatch):
    _patch_adapter_settings(monkeypatch)
    _store_memory_state(
        "resp_prev",
        instructions="stable system",
        messages=[{"role": "system", "content": "stable system"}],
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            ResponsesChatAdapterService.prepare_request(
                {
                    "model": "gpt-4o",
                    "previous_response_id": "resp_prev",
                    "instructions": "new system",
                }
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "responses_chat_adapter_instruction_mismatch"


def test_pending_tool_outputs_are_inserted_before_new_user_message(monkeypatch):
    _patch_adapter_settings(monkeypatch)
    _store_memory_state(
        "resp_prev",
        messages=[
            {"role": "user", "content": "call tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
        ],
        pending=["call_1"],
        tool_round_count=1,
    )

    prepared = asyncio.run(
        ResponsesChatAdapterService.prepare_request(
            {
                "model": "gpt-4o",
                "previous_response_id": "resp_prev",
                "input": [
                    {"type": "function_call_output", "call_id": "call_1", "output": {"ok": True}},
                    {"role": "user", "content": "continue"},
                ],
            }
        )
    )

    assert [item["role"] for item in prepared.messages_before_response[-2:]] == ["tool", "user"]
    assert prepared.messages_before_response[-2]["tool_call_id"] == "call_1"


def test_text_format_json_schema_maps_to_chat_response_format(monkeypatch):
    _patch_adapter_settings(monkeypatch)

    prepared = asyncio.run(
        ResponsesChatAdapterService.prepare_request(
            {
                "model": "gpt-4o",
                "input": "return json",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "answer",
                        "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                        "strict": True,
                    }
                },
            }
        )
    )

    assert prepared.chat_payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            "strict": True,
        },
    }


def test_reasoning_input_items_are_rejected_instead_of_silently_dropped(monkeypatch):
    _patch_adapter_settings(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            ResponsesChatAdapterService.prepare_request(
                {
                    "model": "gpt-4o",
                    "input": [{"type": "reasoning", "summary": []}],
                }
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "responses_chat_adapter_reasoning_item_unsupported"

