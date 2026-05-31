import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)


@dataclass(slots=True)
class MockSettings:
    host: str
    port: int
    model_name: str
    delay_ms: int
    stream_chunks: int
    stream_chunk_delay_ms: int
    output_chars: int


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


SETTINGS = MockSettings(
    host=os.getenv("MOCK_OPENAI_HOST", "127.0.0.1"),
    port=_env_int("MOCK_OPENAI_PORT", 18081),
    model_name=os.getenv("MOCK_OPENAI_MODEL_NAME", "gpt-4o-mini"),
    delay_ms=_env_int("MOCK_OPENAI_DELAY_MS", 50),
    stream_chunks=_env_int("MOCK_OPENAI_STREAM_CHUNKS", 8),
    stream_chunk_delay_ms=_env_int("MOCK_OPENAI_STREAM_CHUNK_DELAY_MS", 20),
    output_chars=_env_int("MOCK_OPENAI_OUTPUT_CHARS", 512),
)

app = FastAPI(title="mock-openai-upstream")


def _build_output_text() -> str:
    return "x" * max(1, SETTINGS.output_chars)


def _usage_from_text(prompt_text: str, completion_text: str) -> dict[str, int]:
    prompt_tokens = max(1, len(prompt_text) // 4)
    completion_tokens = max(1, len(completion_text) // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _extract_chat_prompt(payload: dict[str, Any]) -> str:
    pieces: list[str] = []
    for item in payload.get("messages", []):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str):
            pieces.append(content)
    return " ".join(pieces) or "benchmark"


def _extract_responses_prompt(payload: dict[str, Any]) -> str:
    value = payload.get("input")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [item for item in value if isinstance(item, str)]
        if parts:
            return " ".join(parts)
    return "benchmark"


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": SETTINGS.model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "mock-upstream",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    prompt_text = _extract_chat_prompt(payload)
    completion_text = _build_output_text()
    usage = _usage_from_text(prompt_text, completion_text)
    created = int(time.time())
    response_id = f"chatcmpl-{uuid4().hex}"

    if payload.get("stream") is True:
        async def event_stream():
            await asyncio.sleep(max(0, SETTINGS.delay_ms) / 1000)
            chunk_count = max(1, SETTINGS.stream_chunks)
            chunk_size = max(1, len(completion_text) // chunk_count)
            index = 0
            while index < len(completion_text):
                part = completion_text[index:index + chunk_size]
                index += chunk_size
                chunk_payload = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": str(payload.get("model") or SETTINGS.model_name),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": part},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk_payload, ensure_ascii=False)}\n\n".encode("utf-8")
                await asyncio.sleep(max(0, SETTINGS.stream_chunk_delay_ms) / 1000)
            final_payload = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": str(payload.get("model") or SETTINGS.model_name),
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }
            yield f"data: {json.dumps(final_payload, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "x-request-id": response_id,
            },
        )

    await asyncio.sleep(max(0, SETTINGS.delay_ms) / 1000)
    return JSONResponse(
        {
            "id": response_id,
            "object": "chat.completion",
            "created": created,
            "model": str(payload.get("model") or SETTINGS.model_name),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": completion_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        },
        headers={"x-request-id": response_id},
    )


@app.post("/v1/responses")
async def responses(request: Request):
    payload = await request.json()
    prompt_text = _extract_responses_prompt(payload)
    completion_text = _build_output_text()
    usage = _usage_from_text(prompt_text, completion_text)
    created = int(time.time())
    response_id = f"resp_{uuid4().hex}"

    if payload.get("stream") is True:
        async def event_stream():
            await asyncio.sleep(max(0, SETTINGS.delay_ms) / 1000)
            chunk_count = max(1, SETTINGS.stream_chunks)
            chunk_size = max(1, len(completion_text) // chunk_count)
            index = 0
            while index < len(completion_text):
                part = completion_text[index:index + chunk_size]
                index += chunk_size
                event_payload = {
                    "type": "response.output_text.delta",
                    "response_id": response_id,
                    "delta": part,
                }
                yield f"event: response.output_text.delta\ndata: {json.dumps(event_payload, ensure_ascii=False)}\n\n".encode("utf-8")
                await asyncio.sleep(max(0, SETTINGS.stream_chunk_delay_ms) / 1000)
            completed_payload = {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "model": str(payload.get("model") or SETTINGS.model_name),
                    "status": "completed",
                    "output_text": completion_text,
                    "usage": usage,
                },
            }
            yield f"event: response.completed\ndata: {json.dumps(completed_payload, ensure_ascii=False)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "x-request-id": response_id,
            },
        )

    await asyncio.sleep(max(0, SETTINGS.delay_ms) / 1000)
    return JSONResponse(
        {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "model": str(payload.get("model") or SETTINGS.model_name),
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": completion_text}],
                }
            ],
            "output_text": completion_text,
            "usage": usage,
        },
        headers={"x-request-id": response_id},
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible upstream server for local load tests.")
    parser.add_argument("--host", default=SETTINGS.host)
    parser.add_argument("--port", type=int, default=SETTINGS.port)
    parser.add_argument("--model-name", default=SETTINGS.model_name)
    parser.add_argument("--delay-ms", type=int, default=SETTINGS.delay_ms)
    parser.add_argument("--stream-chunks", type=int, default=SETTINGS.stream_chunks)
    parser.add_argument("--stream-chunk-delay-ms", type=int, default=SETTINGS.stream_chunk_delay_ms)
    parser.add_argument("--output-chars", type=int, default=SETTINGS.output_chars)
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    SETTINGS.host = args.host
    SETTINGS.port = args.port
    SETTINGS.model_name = args.model_name
    SETTINGS.delay_ms = max(0, args.delay_ms)
    SETTINGS.stream_chunks = max(1, args.stream_chunks)
    SETTINGS.stream_chunk_delay_ms = max(0, args.stream_chunk_delay_ms)
    SETTINGS.output_chars = max(1, args.output_chars)
    os.environ["MOCK_OPENAI_HOST"] = SETTINGS.host
    os.environ["MOCK_OPENAI_PORT"] = str(SETTINGS.port)
    os.environ["MOCK_OPENAI_MODEL_NAME"] = SETTINGS.model_name
    os.environ["MOCK_OPENAI_DELAY_MS"] = str(SETTINGS.delay_ms)
    os.environ["MOCK_OPENAI_STREAM_CHUNKS"] = str(SETTINGS.stream_chunks)
    os.environ["MOCK_OPENAI_STREAM_CHUNK_DELAY_MS"] = str(SETTINGS.stream_chunk_delay_ms)
    os.environ["MOCK_OPENAI_OUTPUT_CHARS"] = str(SETTINGS.output_chars)
    pythonpath = os.environ.get("PYTHONPATH", "")
    path_items = [item for item in pythonpath.split(os.pathsep) if item]
    prepend_items = [item for item in (SCRIPT_DIR, PROJECT_ROOT) if item not in path_items]
    if prepend_items:
        os.environ["PYTHONPATH"] = os.pathsep.join(prepend_items + path_items)

    import uvicorn

    target = "mock_openai_upstream:app" if args.workers > 1 else app
    uvicorn.run(target, host=SETTINGS.host, port=SETTINGS.port, log_level="warning", workers=max(1, args.workers))


if __name__ == "__main__":
    main()
