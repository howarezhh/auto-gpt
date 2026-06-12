from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from app.utils.timezone import now_beijing


class NativeProtocolAdapter:
    GEMINI = "gemini"
    CLAUDE_MESSAGES = "claude_messages"
    NATIVE_PROTOCOLS = {GEMINI, CLAUDE_MESSAGES}

    @staticmethod
    def request_path(
        protocol_type: str,
        model_name: str,
        *,
        stream: bool = False,
        endpoint_path_template: str | None = None,
    ) -> str:
        custom_path = NativeProtocolAdapter._custom_request_path(
            endpoint_path_template,
            model_name=model_name,
            stream=stream,
        )
        if custom_path:
            return custom_path
        model = quote(str(model_name or "").strip(), safe="")
        if protocol_type == NativeProtocolAdapter.GEMINI:
            action = "streamGenerateContent?alt=sse" if stream else "generateContent"
            return f"/models/{model}:{action}"
        if protocol_type == NativeProtocolAdapter.CLAUDE_MESSAGES:
            return "/v1/messages"
        raise ValueError(f"unsupported native protocol: {protocol_type}")

    @staticmethod
    def _custom_request_path(endpoint_path_template: str | None, *, model_name: str, stream: bool = False) -> str | None:
        template = str(endpoint_path_template or "").strip()
        if not template:
            return None
        model = quote(str(model_name or "").strip(), safe="")
        raw_model = str(model_name or "").strip()
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        path = (
            template
            .replace("{model}", model)
            .replace("{model_name}", model)
            .replace("{raw_model}", raw_model)
            .replace("{action}", action)
        )
        return path if path.startswith("/") else f"/{path}"

    @staticmethod
    def headers(protocol_type: str, api_key: str) -> dict[str, str]:
        if protocol_type == NativeProtocolAdapter.GEMINI:
            return {"x-goog-api-key": api_key}
        if protocol_type == NativeProtocolAdapter.CLAUDE_MESSAGES:
            return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        return {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    def native_text_payload(
        protocol_type: str,
        *,
        model_name: str,
        prompt: str,
        max_tokens: int = 16,
        stream: bool = False,
    ) -> dict[str, Any]:
        if protocol_type == NativeProtocolAdapter.GEMINI:
            return {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": max(1, int(max_tokens or 16))},
            }
        if protocol_type == NativeProtocolAdapter.CLAUDE_MESSAGES:
            payload: dict[str, Any] = {
                "model": model_name,
                "max_tokens": max(1, int(max_tokens or 16)),
                "temperature": 0,
                "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            }
            if stream:
                payload["stream"] = True
            return payload
        raise ValueError(f"unsupported native protocol: {protocol_type}")

    @staticmethod
    def openai_to_native_payload(protocol_type: str, endpoint_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if protocol_type == NativeProtocolAdapter.GEMINI:
            return NativeProtocolAdapter._openai_to_gemini_payload(endpoint_path, payload)
        if protocol_type == NativeProtocolAdapter.CLAUDE_MESSAGES:
            return NativeProtocolAdapter._openai_to_claude_payload(endpoint_path, payload)
        return payload

    @staticmethod
    def native_response_to_openai(
        protocol_type: str,
        endpoint_path: str,
        response_payload: dict[str, Any],
        *,
        requested_model: str,
    ) -> dict[str, Any]:
        chat_payload = (
            NativeProtocolAdapter._gemini_response_to_chat(response_payload, requested_model=requested_model)
            if protocol_type == NativeProtocolAdapter.GEMINI
            else NativeProtocolAdapter._claude_response_to_chat(response_payload, requested_model=requested_model)
        )
        if endpoint_path == "/responses":
            return NativeProtocolAdapter._chat_to_responses(chat_payload, requested_model=requested_model)
        return chat_payload

    @staticmethod
    def native_stream_chunk_to_chat_chunks(
        protocol_type: str,
        chunk: bytes,
        *,
        requested_model: str,
        state: dict[str, Any],
    ) -> list[bytes]:
        events = NativeProtocolAdapter._parse_sse_json_events(chunk)
        chunks: list[bytes] = []
        for event in events:
            if protocol_type == NativeProtocolAdapter.GEMINI:
                text, finish_reason, usage = NativeProtocolAdapter._extract_gemini_stream_delta(event)
            else:
                text, finish_reason, usage = NativeProtocolAdapter._extract_claude_stream_delta(event)
            usage = NativeProtocolAdapter._merge_stream_usage_state(state, usage)
            if not text and not finish_reason and not usage:
                continue
            chunks.append(
                NativeProtocolAdapter._chat_stream_chunk(
                    requested_model=requested_model,
                    text=text,
                    finish_reason=finish_reason,
                    usage=usage,
                    state=state,
                )
            )
        return chunks

    @staticmethod
    def _openai_to_gemini_payload(endpoint_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        messages = NativeProtocolAdapter._messages_from_openai_payload(endpoint_path, payload)
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            parts = NativeProtocolAdapter._gemini_parts_from_content(message.get("content"))
            if role == "system":
                system_parts.extend(parts)
                continue
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts or [{"text": ""}]})
        native: dict[str, Any] = {"contents": contents or [{"role": "user", "parts": [{"text": "ping"}]}]}
        generation_config: dict[str, Any] = {}
        for source_key, target_key in (
            ("temperature", "temperature"),
            ("top_p", "topP"),
            ("max_tokens", "maxOutputTokens"),
            ("max_completion_tokens", "maxOutputTokens"),
            ("max_output_tokens", "maxOutputTokens"),
            ("stop", "stopSequences"),
        ):
            if source_key in payload:
                generation_config[target_key] = payload[source_key]
        if generation_config:
            native["generationConfig"] = generation_config
        if system_parts:
            native["systemInstruction"] = {"parts": system_parts}
        cached_content = payload.get("cachedContent") or payload.get("cached_content")
        if isinstance(cached_content, str) and cached_content.strip():
            native["cachedContent"] = cached_content.strip()
        return native

    @staticmethod
    def _openai_to_claude_payload(endpoint_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        messages = NativeProtocolAdapter._messages_from_openai_payload(endpoint_path, payload)
        system_parts: list[dict[str, Any]] = []
        claude_messages: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            if role == "system":
                system_parts.extend(NativeProtocolAdapter._claude_blocks_from_content(message.get("content")))
                continue
            claude_messages.append(
                {
                    "role": "assistant" if role == "assistant" else "user",
                    "content": NativeProtocolAdapter._claude_blocks_from_content(message.get("content")),
                }
            )
        native: dict[str, Any] = {
            "model": payload.get("model"),
            "max_tokens": int(payload.get("max_tokens") or payload.get("max_completion_tokens") or payload.get("max_output_tokens") or 1024),
            "messages": claude_messages or [{"role": "user", "content": [{"type": "text", "text": "ping"}]}],
        }
        if system_parts:
            native["system"] = (
                "\n".join(str(part.get("text") or "") for part in system_parts if isinstance(part, dict) and part.get("type") == "text")
                if not NativeProtocolAdapter._blocks_have_cache_control(system_parts)
                else system_parts
            )
        explicit_cache_control = payload.get("cache_control")
        if isinstance(explicit_cache_control, dict):
            native["cache_control"] = dict(explicit_cache_control)
        elif not NativeProtocolAdapter._claude_payload_has_cache_control(native):
            native["cache_control"] = {"type": "ephemeral"}
        for key in ("temperature", "top_p", "stop_sequences"):
            if key in payload:
                native[key] = payload[key]
        if "stop" in payload and "stop_sequences" not in native:
            native["stop_sequences"] = payload["stop"] if isinstance(payload["stop"], list) else [payload["stop"]]
        if payload.get("stream") is True:
            native["stream"] = True
        return native

    @staticmethod
    def _messages_from_openai_payload(endpoint_path: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if endpoint_path == "/responses":
            input_value = payload.get("input")
            if isinstance(input_value, str):
                return [{"role": "user", "content": input_value}]
            if isinstance(input_value, dict):
                return [{"role": input_value.get("role") or "user", "content": input_value.get("content") or input_value.get("text") or ""}]
            if isinstance(input_value, list):
                messages: list[dict[str, Any]] = []
                for item in input_value:
                    if isinstance(item, str):
                        messages.append({"role": "user", "content": item})
                    elif isinstance(item, dict):
                        messages.append({"role": item.get("role") or "user", "content": item.get("content") or item.get("text") or ""})
                return messages
        messages = payload.get("messages")
        return list(messages) if isinstance(messages, list) else [{"role": "user", "content": payload.get("prompt") or ""}]

    @staticmethod
    def _gemini_parts_from_content(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"text": content}]
        if isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for item in content:
                if isinstance(item, str):
                    parts.append({"text": item})
                elif isinstance(item, dict):
                    item_type = item.get("type")
                    if item_type in {"text", "input_text"}:
                        parts.append({"text": str(item.get("text") or "")})
                    elif item_type in {"image_url", "input_image"}:
                        image_url = item.get("image_url")
                        if isinstance(image_url, dict):
                            image_url = image_url.get("url")
                        if isinstance(image_url, str) and image_url.startswith("data:"):
                            header, _, data = image_url.partition(",")
                            mime_type = header.removeprefix("data:").split(";")[0] or "image/png"
                            parts.append({"inlineData": {"mimeType": mime_type, "data": data}})
                        elif image_url:
                            parts.append({"fileData": {"fileUri": str(image_url)}})
            return parts
        return [{"text": NativeProtocolAdapter._content_to_text(content)}]

    @staticmethod
    def _claude_blocks_from_content(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if isinstance(content, list):
            blocks: list[dict[str, Any]] = []
            for item in content:
                if isinstance(item, str):
                    blocks.append({"type": "text", "text": item})
                elif isinstance(item, dict):
                    item_type = item.get("type")
                    if item_type in {"text", "input_text"}:
                        block = {"type": "text", "text": str(item.get("text") or "")}
                        NativeProtocolAdapter._copy_cache_control(item, block)
                        blocks.append(block)
                    elif item_type in {"image_url", "input_image"}:
                        image_url = item.get("image_url")
                        if isinstance(image_url, dict):
                            image_url = image_url.get("url")
                        if isinstance(image_url, str) and image_url.startswith("data:image/"):
                            header, _, data = image_url.partition(",")
                            media_type = header.removeprefix("data:").split(";")[0] or "image/png"
                            block = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
                            NativeProtocolAdapter._copy_cache_control(item, block)
                            blocks.append(block)
            return blocks
        return [{"type": "text", "text": NativeProtocolAdapter._content_to_text(content)}]

    @staticmethod
    def _copy_cache_control(source: dict[str, Any], target: dict[str, Any]) -> None:
        cache_control = source.get("cache_control")
        if isinstance(cache_control, dict):
            target["cache_control"] = dict(cache_control)

    @staticmethod
    def _blocks_have_cache_control(blocks: list[dict[str, Any]]) -> bool:
        return any(isinstance(block, dict) and isinstance(block.get("cache_control"), dict) for block in blocks)

    @staticmethod
    def _claude_payload_has_cache_control(payload: dict[str, Any]) -> bool:
        if isinstance(payload.get("cache_control"), dict):
            return True
        system = payload.get("system")
        if isinstance(system, list) and NativeProtocolAdapter._blocks_have_cache_control(system):
            return True
        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, list) and NativeProtocolAdapter._blocks_have_cache_control(content):
                    return True
        return False

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or ""))
            return "\n".join(part for part in parts if part)
        return "" if content is None else str(content)

    @staticmethod
    def _gemini_response_to_chat(payload: dict[str, Any], *, requested_model: str) -> dict[str, Any]:
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        content = candidate.get("content") if isinstance(candidate, dict) else {}
        text = NativeProtocolAdapter._parts_text(content.get("parts") if isinstance(content, dict) else [])
        finish_reason = str(candidate.get("finishReason") or "stop").lower() if isinstance(candidate, dict) else "stop"
        usage_metadata = payload.get("usageMetadata") if isinstance(payload, dict) else {}
        usage = NativeProtocolAdapter._gemini_usage_to_openai(usage_metadata)
        return NativeProtocolAdapter._chat_completion_payload(requested_model=requested_model, text=text, finish_reason=finish_reason, usage=usage)

    @staticmethod
    def _claude_response_to_chat(payload: dict[str, Any], *, requested_model: str) -> dict[str, Any]:
        text = ""
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, list):
            text = "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text")
        usage_payload = payload.get("usage") if isinstance(payload, dict) else {}
        usage = NativeProtocolAdapter._claude_usage_to_openai(usage_payload)
        return NativeProtocolAdapter._chat_completion_payload(
            requested_model=requested_model,
            text=text,
            finish_reason=str(payload.get("stop_reason") or "stop"),
            usage=usage,
        )

    @staticmethod
    def _usage_int(payload: dict[str, Any], key: str) -> int:
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return max(0, int(value))
        return 0

    @staticmethod
    def _gemini_usage_to_openai(usage_metadata: Any) -> dict[str, Any]:
        usage_metadata = usage_metadata if isinstance(usage_metadata, dict) else {}
        prompt_tokens = NativeProtocolAdapter._usage_int(usage_metadata, "promptTokenCount")
        completion_tokens = NativeProtocolAdapter._usage_int(usage_metadata, "candidatesTokenCount")
        total_tokens = NativeProtocolAdapter._usage_int(usage_metadata, "totalTokenCount") or (prompt_tokens + completion_tokens)
        cached_tokens = NativeProtocolAdapter._usage_int(usage_metadata, "cachedContentTokenCount")
        thoughts_tokens = NativeProtocolAdapter._usage_int(usage_metadata, "thoughtsTokenCount")
        usage: dict[str, Any] = {
            "usage_schema": "gemini_usage_metadata",
            "prompt_tokens": prompt_tokens,
            "input_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cache_read_tokens": cached_tokens,
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
            "native_usage": {"provider": "gemini", "usageMetadata": usage_metadata},
        }
        if thoughts_tokens:
            usage["reasoning_tokens"] = thoughts_tokens
            usage["completion_tokens_details"] = {"reasoning_tokens": thoughts_tokens}
        return usage

    @staticmethod
    def _claude_usage_to_openai(usage_payload: Any) -> dict[str, Any]:
        usage_payload = usage_payload if isinstance(usage_payload, dict) else {}
        input_tokens = NativeProtocolAdapter._usage_int(usage_payload, "input_tokens")
        output_tokens = NativeProtocolAdapter._usage_int(usage_payload, "output_tokens")
        cache_read = NativeProtocolAdapter._usage_int(usage_payload, "cache_read_input_tokens")
        cache_write = NativeProtocolAdapter._usage_int(usage_payload, "cache_creation_input_tokens")
        prompt_tokens = input_tokens + cache_read + cache_write
        return {
            "usage_schema": "claude_messages_usage",
            "prompt_tokens": prompt_tokens,
            "input_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "output_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "prompt_tokens_details": {
                "cached_tokens": cache_read,
                "cache_creation_tokens": cache_write,
                "input_tokens": input_tokens,
            },
            "native_usage": {"provider": "claude", "usage": usage_payload},
        }

    @staticmethod
    def _merge_stream_usage_state(state: dict[str, Any], usage: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(usage, dict):
            return None
        merged = dict(state.get("usage") or {})
        for key, value in usage.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                nested = dict(merged[key])
                if key == "native_usage" and isinstance(nested.get("usage"), dict) and isinstance(value.get("usage"), dict):
                    native_usage = dict(nested["usage"])
                    native_usage.update(value["usage"])
                    nested["usage"] = native_usage
                    for nested_key, nested_value in value.items():
                        if nested_key != "usage":
                            nested[nested_key] = nested_value
                    merged[key] = nested
                    continue
                nested.update(value)
                merged[key] = nested
            elif value is not None:
                merged[key] = value
        prompt_tokens = merged.get("prompt_tokens")
        completion_tokens = merged.get("completion_tokens")
        if isinstance(prompt_tokens, (int, float)) and isinstance(completion_tokens, (int, float)):
            merged["total_tokens"] = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
        state["usage"] = merged
        return merged

    @staticmethod
    def _chat_completion_payload(*, requested_model: str, text: str, finish_reason: str, usage: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": f"chatcmpl_{uuid4().hex}",
            "object": "chat.completion",
            "created": int(now_beijing().timestamp()),
            "model": requested_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish_reason or "stop"}],
            "usage": usage,
        }

    @staticmethod
    def _chat_to_responses(chat_payload: dict[str, Any], *, requested_model: str) -> dict[str, Any]:
        choice = (chat_payload.get("choices") or [{}])[0]
        message = choice.get("message") if isinstance(choice, dict) else {}
        text = message.get("content") if isinstance(message, dict) else ""
        return {
            "id": f"resp_{uuid4().hex}",
            "object": "response",
            "created_at": int(now_beijing().timestamp()),
            "status": "completed",
            "model": requested_model,
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text or ""}]}],
            "output_text": text or "",
            "usage": chat_payload.get("usage") or {},
        }

    @staticmethod
    def _parts_text(parts: Any) -> str:
        if not isinstance(parts, list):
            return ""
        return "".join(str(item.get("text") or "") for item in parts if isinstance(item, dict))

    @staticmethod
    def _parse_sse_json_events(chunk: bytes) -> list[dict[str, Any]]:
        text = chunk.decode("utf-8", errors="ignore")
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except Exception:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events

    @staticmethod
    def _extract_gemini_stream_delta(event: dict[str, Any]) -> tuple[str, str | None, dict[str, Any] | None]:
        candidates = event.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        content = candidate.get("content") if isinstance(candidate, dict) else {}
        text = NativeProtocolAdapter._parts_text(content.get("parts") if isinstance(content, dict) else [])
        finish_reason = str(candidate.get("finishReason") or "").lower() or None if isinstance(candidate, dict) else None
        usage_metadata = event.get("usageMetadata")
        usage = None
        if isinstance(usage_metadata, dict):
            usage = NativeProtocolAdapter._gemini_usage_to_openai(usage_metadata)
        return text, finish_reason, usage

    @staticmethod
    def _extract_claude_stream_delta(event: dict[str, Any]) -> tuple[str, str | None, dict[str, Any] | None]:
        event_type = str(event.get("type") or "")
        if event_type == "message_start":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else event.get("usage")
            return "", None, NativeProtocolAdapter._claude_usage_to_openai(usage) if isinstance(usage, dict) else None
        if event_type == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            return str(delta.get("text") or ""), None, None
        if event_type == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else None
            usage_delta = None
            if isinstance(usage, dict):
                usage_delta = {
                    "usage_schema": "claude_messages_usage",
                    "completion_tokens": NativeProtocolAdapter._usage_int(usage, "output_tokens"),
                    "output_tokens": NativeProtocolAdapter._usage_int(usage, "output_tokens"),
                    "native_usage": {"provider": "claude", "usage": usage},
                }
            return "", str(delta.get("stop_reason") or "") or None, usage_delta
        return "", None, None

    @staticmethod
    def _chat_stream_chunk(
        *,
        requested_model: str,
        text: str,
        finish_reason: str | None,
        usage: dict[str, Any] | None,
        state: dict[str, Any],
    ) -> bytes:
        stream_id = state.setdefault("id", f"chatcmpl_{uuid4().hex}")
        payload: dict[str, Any] = {
            "id": stream_id,
            "object": "chat.completion.chunk",
            "created": int(now_beijing().timestamp()),
            "model": requested_model,
            "choices": [{"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish_reason}],
        }
        if usage:
            payload["usage"] = usage
        return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")
