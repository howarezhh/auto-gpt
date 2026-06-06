from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import SessionLocal
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.responses_chat_adapter_session import ResponsesChatAdapterSession
from app.services.api_key_service import ApiClientAuthContext
from app.services.proxy_service import ProxyService
from app.services.redis_service import RedisService
from app.services.router_service import RoutePolicyContext
from app.services.setting_service import SettingService
from app.utils.json_utils import dumps_json, loads_json, safeJsonParse


ADAPTER_MARKER_KEY = "__aotu_responses_chat_adapter"
ADAPTER_RESPONSE_MODEL_KEY = "__aotu_response_model_override"
ADAPTER_RESPONSE_ID_KEY = "__aotu_response_id_override"


@dataclass(slots=True)
class AdapterConversationState:
    response_id: str | None
    requested_model: str | None
    upstream_model: str | None
    instructions: str | None
    messages: list[dict[str, Any]]
    pending_tool_call_ids: list[str]
    tool_round_count: int = 0


@dataclass(slots=True)
class PreparedAdapterRequest:
    response_id: str
    requested_model: str
    upstream_model: str
    chat_payload: dict[str, Any]
    messages_before_response: list[dict[str, Any]]
    previous_response_id: str | None
    instructions: str | None
    tool_round_count: int
    history_cache_hit: bool
    compacted_history: bool
    truncation_mode: str | None
    compact_threshold: int | None


class ResponsesChatAdapterService:
    """Responses→Chat 单向兼容适配层。"""

    _memory_sessions: dict[str, tuple[dict[str, Any], float | None]] = {}

    @staticmethod
    def enabled() -> bool:
        return bool(ResponsesChatAdapterService._setting_value("responses_chat_adapter_enabled", False))

    @staticmethod
    def sync_env_upstreams(db: Session) -> None:
        specs = ResponsesChatAdapterService._env_upstream_specs()
        if not specs:
            return
        changed = False
        grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
        for spec in specs:
            grouped.setdefault((spec["base_url"], spec["api_key"]), []).append(spec)
        for index, ((base_url, api_key), items) in enumerate(grouped.items(), start=1):
            provider_name = f"Responses→Chat 环境上游 {index}"
            provider = db.scalar(select(Provider).where(Provider.name == provider_name))
            if provider is None:
                provider = Provider(
                    name=provider_name,
                    base_url=base_url,
                    api_key=api_key,
                    provider_type="openai_compatible",
                    protocol_type="chat_completions",
                    enabled=True,
                    remark="由 Responses→Chat 兼容适配设置自动维护",
                )
                db.add(provider)
                db.flush()
                changed = True
            else:
                for field, value in (
                    ("base_url", base_url),
                    ("api_key", api_key),
                    ("provider_type", "openai_compatible"),
                    ("protocol_type", "chat_completions"),
                    ("enabled", True),
                ):
                    if getattr(provider, field) != value:
                        setattr(provider, field, value)
                        changed = True
            existing_models = {item.model_name: item for item in provider.provider_models}
            for item in items:
                model_name = item["upstream_model"]
                provider_model = existing_models.get(model_name)
                if provider_model is None:
                    provider_model = ProviderModel(
                        provider=provider,
                        model_name=model_name,
                        enabled=True,
                        supports_stream=True,
                        supports_vision=True,
                        supports_tools=True,
                        supports_chat_completions=True,
                        supports_responses=False,
                    )
                    db.add(provider_model)
                    changed = True
                else:
                    for field, value in (
                        ("enabled", True),
                        ("supports_stream", True),
                        ("supports_tools", True),
                        ("supports_chat_completions", True),
                    ):
                        if getattr(provider_model, field) != value:
                            setattr(provider_model, field, value)
                            changed = True
            from app.services.provider_service import ProviderService

            ProviderService.refresh_provider_state(provider)
        if changed:
            db.commit()
            from app.services.provider_service import ProviderService

            ProviderService.invalidate_provider_runtime_cache()

    @staticmethod
    async def forward_json_response(
        *,
        payload: dict[str, Any],
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Any, list[dict], int]:
        prepared = await ResponsesChatAdapterService.prepare_request(payload)
        upstream_chat_payload = dict(prepared.chat_payload)
        upstream_chat_payload.pop(ADAPTER_MARKER_KEY, None)
        upstream_chat_payload.pop(ADAPTER_RESPONSE_MODEL_KEY, None)
        upstream_chat_payload.pop(ADAPTER_RESPONSE_ID_KEY, None)
        chat_response, provider, trace, latency_ms = await ProxyService.forward_json_request(
            endpoint_path="/chat/completions",
            payload=upstream_chat_payload,
            log_type="responses",
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_for_log="/v1/responses",
            public_endpoint_path="/responses",
            conversation_key_override=prepared.response_id,
            session_id_override=prepared.response_id,
            route_retry_trace=[ResponsesChatAdapterService._trace_item(prepared, stream=False)],
        )
        response_payload = ProxyService._convert_chat_completion_to_responses_payload(
            chat_response,
            requested_model=prepared.requested_model,
        )
        response_payload["id"] = prepared.response_id
        response_payload["model"] = prepared.requested_model
        await ResponsesChatAdapterService.persist_response(
            prepared=prepared,
            responses_payload=response_payload,
            chat_response=chat_response,
        )
        return response_payload, provider, trace, latency_ms

    @staticmethod
    async def forward_stream_response(
        *,
        payload: dict[str, Any],
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[AsyncIterator[bytes], Any, list[dict], int]:
        prepared = await ResponsesChatAdapterService.prepare_request(payload)
        stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
            endpoint_path="/chat/completions",
            payload=prepared.chat_payload,
            log_type="responses",
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_for_log="/v1/responses",
            public_endpoint_path="/responses",
            conversation_key_override=prepared.response_id,
            session_id_override=prepared.response_id,
            route_retry_trace=[ResponsesChatAdapterService._trace_item(prepared, stream=True)],
        )

        async def wrapped_stream() -> AsyncIterator[bytes]:
            completed_response: dict[str, Any] | None = None
            try:
                async for chunk in stream:
                    completed = ResponsesChatAdapterService._extract_completed_response_from_sse_chunk(chunk)
                    if completed is not None:
                        completed_response = completed
                        completed_response["id"] = prepared.response_id
                        completed_response["model"] = prepared.requested_model
                        chunk = ResponsesChatAdapterService._replace_completed_response_in_sse_chunk(
                            chunk,
                            completed_response,
                        )
                    yield chunk
                if completed_response is not None:
                    await ResponsesChatAdapterService.persist_response(
                        prepared=prepared,
                        responses_payload=completed_response,
                        chat_response=None,
                    )
            except Exception as exc:
                yield ProxyService._format_sse_event(
                    {
                        "type": "response.failed",
                        "response": {
                            "id": prepared.response_id,
                            "object": "response",
                            "status": "failed",
                            "model": prepared.requested_model,
                            "error": {
                                "code": "responses_chat_adapter_stream_failed",
                                "message": str(exc),
                            },
                        },
                    }
                )
                yield b"data: [DONE]\n\n"
                return

        return wrapped_stream(), provider, trace, latency_ms

    @staticmethod
    async def prepare_request(payload: dict[str, Any]) -> PreparedAdapterRequest:
        ResponsesChatAdapterService._reject_unsupported_builtin_tools(payload)
        previous_response_id = payload.get("previous_response_id")
        previous_state = None
        history_cache_hit = False
        if isinstance(previous_response_id, str) and previous_response_id.strip():
            previous_state = await ResponsesChatAdapterService.load_state(previous_response_id.strip())
            if previous_state is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "message": "previous_response_id was not found in Responses→Chat adapter session storage",
                        "code": "responses_chat_previous_response_not_found",
                        "previous_response_id": previous_response_id,
                    },
                )
            history_cache_hit = True
        response_id = f"resp_{uuid4().hex}"
        requested_model = str(payload.get("model") or (previous_state.requested_model if previous_state else "") or "")
        if not requested_model.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "Responses request model is required", "code": "missing_model"},
            )
        upstream_model = ResponsesChatAdapterService._mapped_model(requested_model)
        base_messages = [ResponsesChatAdapterService._copy_message(item) for item in (previous_state.messages if previous_state else [])]
        instructions = previous_state.instructions if previous_state else None
        if previous_state is None:
            incoming_instructions = payload.get("instructions")
            if isinstance(incoming_instructions, str) and incoming_instructions.strip():
                instructions = incoming_instructions
                base_messages.append({"role": "system", "content": incoming_instructions})
        new_messages = ResponsesChatAdapterService._input_to_chat_messages(
            payload.get("input"),
            pending_tool_call_ids=previous_state.pending_tool_call_ids if previous_state else [],
        )
        adapter_payload = dict(payload)
        new_messages = await ResponsesChatAdapterService._maybe_apply_web_search_proxy(adapter_payload, new_messages)
        raw_messages = base_messages + new_messages
        truncation_mode = str(adapter_payload.get("truncation") or "").strip().lower() or None
        compact_threshold = ResponsesChatAdapterService._compact_threshold_from_context_management(adapter_payload.get("context_management"))
        messages = ResponsesChatAdapterService._maybe_compact_history_once(
            raw_messages,
            compact_threshold=compact_threshold,
            truncation_mode=truncation_mode,
        )
        compacted_history = len(messages) != len(raw_messages) or (
            bool(messages)
            and bool(raw_messages)
            and messages[0] != raw_messages[0]
        )
        tool_round_count = int(previous_state.tool_round_count if previous_state else 0)
        if new_messages and all(item.get("role") == "tool" for item in new_messages):
            max_tool_rounds = int(ResponsesChatAdapterService._setting_value("responses_chat_adapter_max_tool_rounds", 10) or 10)
            if tool_round_count >= max_tool_rounds:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "message": "Responses→Chat adapter maximum tool-call rounds exceeded",
                        "code": "responses_chat_adapter_tool_round_limit_exceeded",
                        "max_tool_rounds": max_tool_rounds,
                    },
                )
        chat_payload = ResponsesChatAdapterService._build_chat_payload(
            adapter_payload,
            messages,
            upstream_model,
            requested_model,
            response_id,
        )
        return PreparedAdapterRequest(
            response_id=response_id,
            requested_model=requested_model,
            upstream_model=upstream_model,
            chat_payload=chat_payload,
            messages_before_response=messages,
            previous_response_id=previous_response_id if isinstance(previous_response_id, str) else None,
            instructions=instructions,
            tool_round_count=tool_round_count,
            history_cache_hit=history_cache_hit,
            compacted_history=compacted_history,
            truncation_mode=truncation_mode,
            compact_threshold=compact_threshold,
        )

    @staticmethod
    def _trace_item(prepared: PreparedAdapterRequest, *, stream: bool) -> dict[str, Any]:
        return {
            "result": "responses_chat_adapter",
            "mode": "responses_to_chat_stream" if stream else "responses_to_chat",
            "requested_model": prepared.requested_model,
            "upstream_model": prepared.upstream_model,
            "response_id": prepared.response_id,
            "previous_response_id": prepared.previous_response_id,
            "history_cache_hit": prepared.history_cache_hit,
            "storage_type": ResponsesChatAdapterService._storage_type(),
            "cache_prefix_policy": "immutable_history_prefix",
            "compacted_history": prepared.compacted_history,
            "truncation_mode": prepared.truncation_mode,
            "compact_threshold": prepared.compact_threshold,
        }

    @staticmethod
    async def persist_response(
        *,
        prepared: PreparedAdapterRequest,
        responses_payload: dict[str, Any],
        chat_response: dict[str, Any] | None,
    ) -> None:
        assistant_message = (
            ResponsesChatAdapterService._assistant_message_from_chat_response(chat_response)
            if chat_response is not None
            else ResponsesChatAdapterService._assistant_message_from_responses_payload(responses_payload)
        )
        messages = [ResponsesChatAdapterService._copy_message(item) for item in prepared.messages_before_response]
        if assistant_message is not None:
            messages.append(assistant_message)
        pending_tool_call_ids = ResponsesChatAdapterService._tool_call_ids_from_assistant(assistant_message)
        tool_round_count = prepared.tool_round_count + (1 if pending_tool_call_ids else 0)
        await ResponsesChatAdapterService.save_state(
            AdapterConversationState(
                response_id=prepared.response_id,
                requested_model=prepared.requested_model,
                upstream_model=prepared.upstream_model,
                instructions=prepared.instructions,
                messages=messages,
                pending_tool_call_ids=pending_tool_call_ids,
                tool_round_count=tool_round_count,
            ),
            previous_response_id=prepared.previous_response_id,
            truncation_mode=prepared.truncation_mode,
            compact_threshold=prepared.compact_threshold,
        )

    @staticmethod
    def _build_chat_payload(
        payload: dict[str, Any],
        messages: list[dict[str, Any]],
        upstream_model: str,
        requested_model: str,
        response_id: str,
    ) -> dict[str, Any]:
        chat_payload: dict[str, Any] = {
            "model": upstream_model,
            "messages": messages,
            ADAPTER_MARKER_KEY: True,
            ADAPTER_RESPONSE_MODEL_KEY: requested_model,
            ADAPTER_RESPONSE_ID_KEY: response_id,
        }
        passthrough_keys = {
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "stream",
            "user",
            "metadata",
            "seed",
        }
        for key in passthrough_keys:
            if key in payload:
                chat_payload[key] = payload[key]
        if "tools" in chat_payload:
            chat_payload["tools"] = ProxyService._normalize_responses_tools_for_chat(chat_payload.get("tools"))
        if "tool_choice" in chat_payload:
            chat_payload["tool_choice"] = ProxyService._normalize_responses_tool_choice_for_chat(chat_payload.get("tool_choice"))
        if "max_output_tokens" in payload:
            chat_payload["max_completion_tokens"] = payload["max_output_tokens"]
        elif "max_tokens" in payload:
            chat_payload["max_tokens"] = payload["max_tokens"]
        return chat_payload

    @staticmethod
    def _input_to_chat_messages(input_value: Any, *, pending_tool_call_ids: list[str]) -> list[dict[str, Any]]:
        items = input_value if isinstance(input_value, list) else [input_value]
        if input_value is None:
            return [{"role": "user", "content": ""}]
        tool_outputs: dict[str, dict[str, Any]] = {}
        messages: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                call_id = str(item.get("call_id") or item.get("id") or "")
                if not call_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"message": "function_call_output requires call_id", "code": "missing_function_call_output_call_id"},
                    )
                tool_outputs[call_id] = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": ResponsesChatAdapterService._stringify_tool_output(item.get("output")),
                }
                continue
            converted = ResponsesChatAdapterService._convert_input_item_to_chat_message(item)
            if converted is not None:
                messages.append(converted)
        if tool_outputs:
            ordered_ids = [call_id for call_id in pending_tool_call_ids if call_id in tool_outputs]
            ordered_ids.extend(call_id for call_id in tool_outputs if call_id not in ordered_ids)
            messages.extend(tool_outputs[call_id] for call_id in ordered_ids)
        return messages or [{"role": "user", "content": ""}]

    @staticmethod
    def _convert_input_item_to_chat_message(item: Any) -> dict[str, Any] | None:
        if isinstance(item, str):
            return {"role": "user", "content": item}
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "Responses input items must be strings or objects", "code": "invalid_responses_input"},
            )
        item_type = item.get("type")
        if item_type == "message" or "role" in item or "content" in item:
            return ProxyService._convert_responses_input_item_to_chat_message(item)
        if item_type == "input_text" and isinstance(item.get("text"), str):
            return {"role": "user", "content": item["text"]}
        if item_type == "input_image":
            return {
                "role": "user",
                "content": [ProxyService._convert_responses_content_part_to_chat_content(item)],
            }
        if item_type in {"function_call", "reasoning"}:
            return None
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Responses→Chat adapter received an unsupported input item type",
                "code": "responses_chat_adapter_unsupported_input_type",
                "input_type": item_type,
            },
        )

    @staticmethod
    def _reject_unsupported_builtin_tools(payload: dict[str, Any]) -> None:
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_type = str(tool.get("type") or "")
            if tool_type in {"file_search", "code_interpreter"}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "message": f"Responses→Chat adapter does not support built-in tool: {tool_type}",
                        "code": f"responses_chat_adapter_{tool_type}_unsupported",
                    },
                )
            if tool_type in {"web_search", "web_search_preview"} and not ResponsesChatAdapterService._setting_value("responses_chat_adapter_web_search_enabled", False):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "message": "Responses→Chat adapter web_search is disabled. Enable RESPONSES_CHAT_ADAPTER_WEB_SEARCH_ENABLED and configure a search proxy to use it.",
                        "code": "responses_chat_adapter_web_search_disabled",
                    },
                )

    @staticmethod
    async def _maybe_apply_web_search_proxy(
        payload: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return messages
        web_tools = [tool for tool in tools if isinstance(tool, dict) and str(tool.get("type") or "") in {"web_search", "web_search_preview"}]
        if not web_tools:
            return messages
        payload["tools"] = [
            tool
            for tool in tools
            if not (isinstance(tool, dict) and str(tool.get("type") or "") in {"web_search", "web_search_preview"})
        ]
        if not payload["tools"]:
            payload.pop("tools", None)
        settings = ResponsesChatAdapterService._adapter_settings()
        if not settings.responses_chat_adapter_web_search_enabled:
            return messages
        proxy_url = settings.responses_chat_adapter_search_proxy_url.strip()
        if not proxy_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "Responses→Chat adapter web_search proxy is enabled but RESPONSES_CHAT_ADAPTER_SEARCH_PROXY_URL is empty",
                    "code": "responses_chat_adapter_web_search_proxy_not_configured",
                },
            )
        query = ResponsesChatAdapterService._latest_user_text(messages)
        if not query:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "Responses→Chat adapter web_search requires text in the latest user message",
                    "code": "responses_chat_adapter_web_search_query_missing",
                },
            )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(proxy_url, json={"query": query, "tools": web_tools})
                response.raise_for_status()
                result = response.json()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "Responses→Chat adapter web_search proxy request failed",
                    "code": "responses_chat_adapter_web_search_proxy_failed",
                    "error": str(exc),
                },
            ) from exc
        injected = ResponsesChatAdapterService._inject_search_result_into_latest_user(messages, result)
        return injected

    @staticmethod
    def _assistant_message_from_chat_response(chat_response: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(chat_response, dict):
            return None
        choices = chat_response.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            return None
        assistant = {"role": "assistant", "content": message.get("content")}
        if isinstance(message.get("tool_calls"), list):
            assistant["tool_calls"] = message["tool_calls"]
        return assistant

    @staticmethod
    def _assistant_message_from_responses_payload(responses_payload: dict[str, Any]) -> dict[str, Any] | None:
        text = ProxyService._extract_response_text(responses_payload, limit_bytes=1_048_576) or ""
        tool_calls = ProxyService._extract_tool_calls_from_responses_output(responses_payload)
        assistant: dict[str, Any] = {"role": "assistant", "content": text if text else None}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        return assistant

    @staticmethod
    def _tool_call_ids_from_assistant(message: dict[str, Any] | None) -> list[str]:
        if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
            return []
        ids: list[str] = []
        for item in message["tool_calls"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
        return ids

    @staticmethod
    def _latest_user_text(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [
                    str(part.get("text") or "").strip()
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                return "\n".join(part for part in parts if part).strip()
        return ""

    @staticmethod
    def _inject_search_result_into_latest_user(
        messages: list[dict[str, Any]],
        search_result: Any,
    ) -> list[dict[str, Any]]:
        injected = [ResponsesChatAdapterService._copy_message(item) for item in messages]
        search_text = f"\n\n[web_search results]\n{dumps_json(search_result)}"
        for message in reversed(injected):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = f"{content}{search_text}"
                return injected
            if isinstance(content, list):
                message["content"] = list(content) + [{"type": "text", "text": search_text.strip()}]
                return injected
        injected.append({"role": "user", "content": search_text.strip()})
        return injected

    @staticmethod
    def _maybe_compact_history_once(
        messages: list[dict[str, Any]],
        *,
        compact_threshold: int | None = None,
        truncation_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        context_window = int(ResponsesChatAdapterService._setting_value("responses_chat_adapter_context_window_tokens", 0) or 0)
        threshold = compact_threshold if isinstance(compact_threshold, int) and compact_threshold > 0 else context_window
        if threshold <= 0:
            return messages
        if not messages or ResponsesChatAdapterService._has_adapter_summary_system(messages):
            return messages
        if truncation_mode == "disabled" and compact_threshold is None:
            return messages
        estimated_tokens = ResponsesChatAdapterService._estimate_messages_tokens(messages)
        if estimated_tokens < threshold:
            return messages
        return ResponsesChatAdapterService._compact_messages_for_snapshot(messages)

    @staticmethod
    def _compact_messages_for_snapshot(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        system_messages = [item for item in messages if item.get("role") == "system"]
        first_system = system_messages[0] if system_messages else None
        non_system = [item for item in messages if item.get("role") != "system"]
        if len(non_system) <= 4:
            return messages
        preserved_tail = non_system[-4:]
        summarized_prefix = non_system[:-4]
        preserved_user_prefix = [
            ResponsesChatAdapterService._copy_message(item)
            for item in summarized_prefix
            if item.get("role") == "user"
        ]
        summarized_non_user_prefix = [
            item
            for item in summarized_prefix
            if item.get("role") != "user"
        ]
        base_system_text = str(first_system.get("content") or "") if first_system else ""
        summary_text = ResponsesChatAdapterService._deterministic_summary(summarized_non_user_prefix)
        compacted_system = {
            "role": "system",
            "content": (
                f"{base_system_text}\n\n"
                "[Responses→Chat adapter immutable summary]\n"
                "Official Responses compaction produces opaque encrypted compaction items. "
                "This Chat-only compatibility layer preserves older user messages where possible "
                "and summarizes older assistant/tool/reasoning context deterministically.\n"
                f"{summary_text}"
            ).strip(),
        }
        return [compacted_system] + preserved_user_prefix + preserved_tail

    @staticmethod
    def _compact_threshold_from_context_management(value: Any) -> int | None:
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip().lower() != "compaction":
                    continue
                threshold = item.get("compact_threshold")
                if isinstance(threshold, int) and threshold > 0:
                    return threshold
                if isinstance(threshold, str) and threshold.isdigit():
                    parsed = int(threshold)
                    if parsed > 0:
                        return parsed
        elif isinstance(value, dict):
            threshold = value.get("compact_threshold")
            if isinstance(threshold, int) and threshold > 0:
                return threshold
            if isinstance(threshold, str) and threshold.isdigit():
                parsed = int(threshold)
                if parsed > 0:
                    return parsed
        return None

    @staticmethod
    def _has_adapter_summary_system(messages: list[dict[str, Any]]) -> bool:
        return any(
            item.get("role") == "system"
            and isinstance(item.get("content"), str)
            and "[Responses→Chat adapter immutable summary]" in item["content"]
            for item in messages
        )

    @staticmethod
    def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
        text = dumps_json(messages)
        return max(1, len(text) // 4)

    @staticmethod
    def _deterministic_summary(messages: list[dict[str, Any]]) -> str:
        compact_lines: list[str] = []
        for message in messages:
            role = str(message.get("role") or "unknown")
            content = message.get("content")
            if isinstance(content, str):
                text = content
            else:
                text = dumps_json(content)
            if len(text) > 500:
                text = f"{text[:500]}...[truncated]"
            compact_lines.append(f"{role}: {text}")
        summary = "\n".join(compact_lines)
        if len(summary) > 4000:
            summary = f"{summary[:4000]}...[truncated]"
        return summary

    @staticmethod
    async def load_state(response_id: str) -> AdapterConversationState | None:
        storage_type = ResponsesChatAdapterService._storage_type()
        if storage_type == "redis":
            return await ResponsesChatAdapterService._load_state_redis(response_id)
        if storage_type in {"database", "postgresql", "postgres"}:
            return await run_in_threadpool(ResponsesChatAdapterService._load_state_database, response_id)
        return ResponsesChatAdapterService._load_state_memory(response_id)

    @staticmethod
    async def save_state(
        state: AdapterConversationState,
        *,
        previous_response_id: str | None,
        truncation_mode: str | None = None,
        compact_threshold: int | None = None,
    ) -> None:
        storage_type = ResponsesChatAdapterService._storage_type()
        payload = ResponsesChatAdapterService._state_to_payload(state, previous_response_id=previous_response_id)
        payload = ResponsesChatAdapterService._fit_payload_to_snapshot_limit(
            payload,
            compact_threshold=compact_threshold,
            truncation_mode=truncation_mode,
        )
        ttl_seconds = ResponsesChatAdapterService._ttl_seconds()
        if storage_type == "redis":
            await ResponsesChatAdapterService._save_state_redis(str(state.response_id), payload, ttl_seconds)
            return
        if storage_type in {"database", "postgresql", "postgres"}:
            await run_in_threadpool(ResponsesChatAdapterService._save_state_database, payload)
            return
        ResponsesChatAdapterService._save_state_memory(str(state.response_id), payload, ttl_seconds)

    @staticmethod
    def _state_to_payload(state: AdapterConversationState, *, previous_response_id: str | None) -> dict[str, Any]:
        now = int(time.time())
        ttl_seconds = ResponsesChatAdapterService._ttl_seconds()
        return {
            "response_id": state.response_id,
            "previous_response_id": previous_response_id,
            "requested_model": state.requested_model,
            "upstream_model": state.upstream_model,
            "instructions": state.instructions,
            "messages": state.messages,
            "pending_tool_call_ids": state.pending_tool_call_ids,
            "tool_round_count": state.tool_round_count,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + ttl_seconds if ttl_seconds > 0 else None,
        }

    @staticmethod
    def _fit_payload_to_snapshot_limit(
        payload: dict[str, Any],
        *,
        compact_threshold: int | None = None,
        truncation_mode: str | None = None,
    ) -> dict[str, Any]:
        limit = ResponsesChatAdapterService._snapshot_max_bytes()
        if limit <= 0:
            return payload
        encoded = dumps_json(payload).encode("utf-8")
        if len(encoded) <= limit:
            return payload
        if truncation_mode == "disabled" and compact_threshold is None:
            ResponsesChatAdapterService._raise_snapshot_too_large(len(encoded), limit)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            ResponsesChatAdapterService._raise_snapshot_too_large(len(encoded), limit)
        compacted_payload = dict(payload)
        compacted_payload["messages"] = ResponsesChatAdapterService._compact_messages_for_snapshot(
            [ResponsesChatAdapterService._copy_message(item) for item in messages if isinstance(item, dict)]
        )
        compacted_payload["snapshot_compacted"] = True
        compacted_payload["snapshot_original_bytes"] = len(encoded)
        compacted_encoded = dumps_json(compacted_payload).encode("utf-8")
        if len(compacted_encoded) <= limit:
            return compacted_payload
        messages_copy = [ResponsesChatAdapterService._copy_message(item) for item in compacted_payload.get("messages") or [] if isinstance(item, dict)]
        while messages_copy:
            compacted_payload["messages"] = messages_copy
            final_encoded = dumps_json(compacted_payload).encode("utf-8")
            if len(final_encoded) <= limit:
                return compacted_payload
            if len(messages_copy) <= 1:
                break
            messages_copy = ResponsesChatAdapterService._drop_oldest_message(messages_copy)
        final_encoded = dumps_json(compacted_payload).encode("utf-8")
        ResponsesChatAdapterService._raise_snapshot_too_large(len(final_encoded), limit)
        return compacted_payload

    @staticmethod
    def _drop_oldest_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not messages:
            return []
        if messages[0].get("role") == "system":
            if len(messages) <= 1:
                return messages
            return [messages[0]] + messages[2:]
        return messages[1:]

    @staticmethod
    def _raise_snapshot_too_large(actual_bytes: int, limit: int) -> None:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "message": "Responses→Chat adapter session snapshot exceeds configured byte limit",
                "code": "responses_chat_adapter_snapshot_too_large",
                "actual_bytes": actual_bytes,
                "max_bytes": limit,
            },
        )

    @staticmethod
    def _payload_to_state(payload: dict[str, Any] | None) -> AdapterConversationState | None:
        if not isinstance(payload, dict):
            return None
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at > 0 and expires_at < time.time():
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        pending = payload.get("pending_tool_call_ids")
        return AdapterConversationState(
            response_id=str(payload.get("response_id") or ""),
            requested_model=payload.get("requested_model") if isinstance(payload.get("requested_model"), str) else None,
            upstream_model=payload.get("upstream_model") if isinstance(payload.get("upstream_model"), str) else None,
            instructions=payload.get("instructions") if isinstance(payload.get("instructions"), str) else None,
            messages=[ResponsesChatAdapterService._copy_message(item) for item in messages if isinstance(item, dict)],
            pending_tool_call_ids=[str(item) for item in pending] if isinstance(pending, list) else [],
            tool_round_count=int(payload.get("tool_round_count") or 0),
        )

    @staticmethod
    def _load_state_memory(response_id: str) -> AdapterConversationState | None:
        stored = ResponsesChatAdapterService._memory_sessions.get(response_id)
        if stored is None:
            return None
        payload, expires_at = stored
        if expires_at is not None and expires_at < time.time():
            ResponsesChatAdapterService._memory_sessions.pop(response_id, None)
            return None
        return ResponsesChatAdapterService._payload_to_state(payload)

    @staticmethod
    def _save_state_memory(response_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds > 0 else None
        ResponsesChatAdapterService._memory_sessions[response_id] = (payload, expires_at)

    @staticmethod
    async def _load_state_redis(response_id: str) -> AdapterConversationState | None:
        client = RedisService.get_client()
        value = await client.get(ResponsesChatAdapterService._redis_key(response_id))
        return ResponsesChatAdapterService._payload_to_state(loads_json(value, None) if isinstance(value, str) else None)

    @staticmethod
    async def _save_state_redis(response_id: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        client = RedisService.get_client()
        key = ResponsesChatAdapterService._redis_key(response_id)
        if ttl_seconds > 0:
            await client.setex(key, ttl_seconds, dumps_json(payload))
        else:
            await client.set(key, dumps_json(payload))

    @staticmethod
    def _load_state_database(response_id: str) -> AdapterConversationState | None:
        db = SessionLocal()
        try:
            row = db.get(ResponsesChatAdapterSession, response_id)
            if row is None:
                return None
            if row.expires_at is not None and row.expires_at < datetime.utcnow():
                db.delete(row)
                db.commit()
                return None
            return ResponsesChatAdapterService._payload_to_state(
                {
                    "response_id": row.response_id,
                    "requested_model": row.requested_model,
                    "upstream_model": row.upstream_model,
                    "instructions": row.instructions,
                    "messages": loads_json(row.messages_json, []) or [],
                    "pending_tool_call_ids": loads_json(row.pending_tool_call_ids_json, []) or [],
                    "tool_round_count": row.tool_round_count,
                    "expires_at": int(row.expires_at.timestamp()) if row.expires_at else None,
                }
            )
        finally:
            db.close()

    @staticmethod
    def _save_state_database(payload: dict[str, Any]) -> None:
        db = SessionLocal()
        try:
            response_id = str(payload.get("response_id") or "")
            row = db.get(ResponsesChatAdapterSession, response_id)
            if row is None:
                row = ResponsesChatAdapterSession(response_id=response_id, messages_json="[]")
                db.add(row)
            expires_at = payload.get("expires_at")
            row.previous_response_id = payload.get("previous_response_id")
            row.requested_model = payload.get("requested_model")
            row.upstream_model = payload.get("upstream_model")
            row.instructions = payload.get("instructions")
            row.messages_json = dumps_json(payload.get("messages") or [])
            row.pending_tool_call_ids_json = dumps_json(payload.get("pending_tool_call_ids") or [])
            row.tool_round_count = int(payload.get("tool_round_count") or 0)
            row.updated_at = datetime.utcnow()
            row.expires_at = datetime.utcfromtimestamp(expires_at) if isinstance(expires_at, (int, float)) else None
            db.commit()
        finally:
            db.close()

    @staticmethod
    def cleanup_expired_database_sessions(db: Session) -> int:
        result = db.execute(
            delete(ResponsesChatAdapterSession).where(
                ResponsesChatAdapterSession.expires_at.is_not(None),
                ResponsesChatAdapterSession.expires_at < datetime.utcnow(),
            )
        )
        db.commit()
        return int(result.rowcount or 0)

    @staticmethod
    def _extract_completed_response_from_sse_chunk(chunk: bytes) -> dict[str, Any] | None:
        for event in chunk.decode("utf-8", errors="ignore").split("\n\n"):
            for line in event.splitlines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                parsed = safeJsonParse(data)
                if isinstance(parsed, dict) and parsed.get("type") == "response.completed" and isinstance(parsed.get("response"), dict):
                    return parsed["response"]
        return None

    @staticmethod
    def _replace_completed_response_in_sse_chunk(chunk: bytes, response_payload: dict[str, Any]) -> bytes:
        text = chunk.decode("utf-8", errors="ignore")
        parts = []
        replaced = False
        for event in text.split("\n\n"):
            if not event:
                continue
            parsed_event = None
            for line in event.splitlines():
                if line.startswith("data:"):
                    parsed = safeJsonParse(line[5:].strip())
                    if isinstance(parsed, dict) and parsed.get("type") == "response.completed":
                        parsed["response"] = response_payload
                        parsed_event = f"data: {dumps_json(parsed)}"
                        replaced = True
                        break
            parts.append(parsed_event or event)
        if not replaced:
            return chunk
        return ("\n\n".join(parts) + "\n\n").encode("utf-8")

    @staticmethod
    def _mapped_model(model_name: str) -> str:
        upstream_specs = ResponsesChatAdapterService._env_upstream_specs()
        for spec in upstream_specs:
            if spec["requested_model"] == model_name:
                return spec["upstream_model"]
        mapping = safeJsonParse(ResponsesChatAdapterService._setting_value("responses_chat_adapter_model_map_json", "") or "")
        if isinstance(mapping, dict):
            mapped = mapping.get(model_name)
            if isinstance(mapped, str) and mapped.strip():
                return mapped.strip()
        return model_name

    @staticmethod
    def _env_upstream_specs() -> list[dict[str, str]]:
        settings = ResponsesChatAdapterService._adapter_settings()
        specs: list[dict[str, str]] = []
        raw = safeJsonParse(settings.responses_chat_adapter_upstreams_json or "")
        if isinstance(raw, dict):
            iterable = []
            for requested_model, value in raw.items():
                if isinstance(value, dict):
                    iterable.append({"requested_model": requested_model, **value})
                elif isinstance(value, str):
                    iterable.append({"requested_model": requested_model, "upstream_model": value})
        elif isinstance(raw, list):
            iterable = [item for item in raw if isinstance(item, dict)]
        else:
            iterable = []
        for item in iterable:
            requested_model = str(item.get("requested_model") or item.get("model") or "").strip()
            upstream_model = str(item.get("upstream_model") or item.get("target_model") or requested_model).strip()
            base_url = str(item.get("base_url") or settings.responses_chat_adapter_upstream_base_url or "").strip().rstrip("/")
            api_key = str(item.get("api_key") or settings.responses_chat_adapter_upstream_api_key or "").strip()
            if requested_model and upstream_model and base_url and api_key:
                specs.append(
                    {
                        "requested_model": requested_model,
                        "upstream_model": upstream_model,
                        "base_url": base_url,
                        "api_key": api_key,
                    }
                )
        single_base_url = settings.responses_chat_adapter_upstream_base_url.strip().rstrip("/")
        single_api_key = settings.responses_chat_adapter_upstream_api_key.strip()
        mapping = safeJsonParse(settings.responses_chat_adapter_model_map_json or "")
        if single_base_url and single_api_key and isinstance(mapping, dict):
            existing = {(item["requested_model"], item["upstream_model"], item["base_url"]) for item in specs}
            for requested_model, upstream_model in mapping.items():
                if not isinstance(requested_model, str) or not isinstance(upstream_model, str):
                    continue
                key = (requested_model.strip(), upstream_model.strip(), single_base_url)
                if requested_model.strip() and upstream_model.strip() and key not in existing:
                    specs.append(
                        {
                            "requested_model": requested_model.strip(),
                            "upstream_model": upstream_model.strip(),
                            "base_url": single_base_url,
                            "api_key": single_api_key,
                        }
                    )
        return specs

    @staticmethod
    def _storage_type() -> str:
        value = str(ResponsesChatAdapterService._setting_value("responses_chat_adapter_storage_type", "memory") or "memory").strip().lower()
        return value or "memory"

    @staticmethod
    def _ttl_seconds() -> int:
        return max(0, int(ResponsesChatAdapterService._setting_value("responses_chat_adapter_ttl_seconds", 0) or 0))

    @staticmethod
    def _snapshot_max_bytes() -> int:
        return max(0, int(ResponsesChatAdapterService._setting_value("responses_chat_adapter_snapshot_max_bytes", 1048576) or 0))

    @staticmethod
    def _adapter_settings() -> Any:
        try:
            return SettingService.get_cached()
        except Exception:
            return get_settings()

    @staticmethod
    def _setting_value(name: str, default: Any = None) -> Any:
        settings = ResponsesChatAdapterService._adapter_settings()
        if hasattr(settings, name):
            return getattr(settings, name)
        return getattr(get_settings(), name, default)

    @staticmethod
    def _redis_key(response_id: str) -> str:
        return f"responses-chat-adapter-session:{response_id}"

    @staticmethod
    def _copy_message(item: dict[str, Any]) -> dict[str, Any]:
        return loads_json(dumps_json(item), None) or dict(item)

    @staticmethod
    def _stringify_tool_output(value: Any) -> str:
        if isinstance(value, str):
            return value
        return dumps_json(value)
