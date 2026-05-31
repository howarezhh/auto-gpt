import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4


class FastThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4096


class MockConfig:
    model_name = "gpt-4o-mini"
    delay_ms = 0
    stream_chunks = 4
    stream_chunk_delay_ms = 0
    output_chars = 128


def _output_text() -> str:
    return "x" * max(1, int(MockConfig.output_chars))


def _usage(prompt_text: str, completion_text: str) -> dict[str, int]:
    prompt_tokens = max(1, len(prompt_text) // 4)
    completion_tokens = max(1, len(completion_text) // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _chat_prompt(payload: dict[str, Any]) -> str:
    pieces: list[str] = []
    for item in payload.get("messages", []):
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            pieces.append(item["content"])
    return " ".join(pieces) or "benchmark"


def _responses_prompt(payload: dict[str, Any]) -> str:
    value = payload.get("input")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        pieces = [item for item in value if isinstance(item, str)]
        if pieces:
            return " ".join(pieces)
    return "benchmark"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-openai-threaded/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/v1/models":
            self._json({"error": {"message": "not found"}}, status=404)
            return
        self._json(
            {
                "object": "list",
                "data": [
                    {
                        "id": MockConfig.model_name,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "mock-upstream",
                    }
                ],
            }
        )

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        payload = self._read_json()
        if path == "/v1/chat/completions":
            self._chat_completions(payload)
            return
        if path == "/v1/responses":
            self._responses(payload)
            return
        self._json({"error": {"message": "not found"}}, status=404)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        if length <= 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _json(self, payload: dict[str, Any], *, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "keep-alive")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _write_sse(self, item: bytes) -> None:
        self.wfile.write(item)
        self.wfile.flush()

    def _chat_completions(self, payload: dict[str, Any]) -> None:
        prompt_text = _chat_prompt(payload)
        completion_text = _output_text()
        usage = _usage(prompt_text, completion_text)
        created = int(time.time())
        response_id = f"chatcmpl-{uuid4().hex}"
        model = str(payload.get("model") or MockConfig.model_name)
        if payload.get("stream") is True:
            self._stream_headers(response_id)
            self._sleep(MockConfig.delay_ms)
            for part in self._chunks(completion_text):
                chunk_payload = {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
                }
                self._write_sse(f"data: {json.dumps(chunk_payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                self._sleep(MockConfig.stream_chunk_delay_ms)
            final_payload = {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            }
            self._write_sse(f"data: {json.dumps(final_payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            self._write_sse(b"data: [DONE]\n\n")
            return
        self._sleep(MockConfig.delay_ms)
        self._json(
            {
                "id": response_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": completion_text}, "finish_reason": "stop"}],
                "usage": usage,
            },
            extra_headers={"x-request-id": response_id},
        )

    def _responses(self, payload: dict[str, Any]) -> None:
        prompt_text = _responses_prompt(payload)
        completion_text = _output_text()
        usage = _usage(prompt_text, completion_text)
        created = int(time.time())
        response_id = f"resp_{uuid4().hex}"
        model = str(payload.get("model") or MockConfig.model_name)
        if payload.get("stream") is True:
            self._stream_headers(response_id)
            self._sleep(MockConfig.delay_ms)
            for part in self._chunks(completion_text):
                event_payload = {"type": "response.output_text.delta", "response_id": response_id, "delta": part}
                self._write_sse(
                    f"event: response.output_text.delta\ndata: {json.dumps(event_payload, ensure_ascii=False)}\n\n".encode("utf-8")
                )
                self._sleep(MockConfig.stream_chunk_delay_ms)
            completed_payload = {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "model": model,
                    "status": "completed",
                    "output_text": completion_text,
                    "usage": usage,
                },
            }
            self._write_sse(f"event: response.completed\ndata: {json.dumps(completed_payload, ensure_ascii=False)}\n\n".encode("utf-8"))
            self._write_sse(b"data: [DONE]\n\n")
            return
        self._sleep(MockConfig.delay_ms)
        self._json(
            {
                "id": response_id,
                "object": "response",
                "created_at": created,
                "model": model,
                "status": "completed",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": completion_text}]}],
                "output_text": completion_text,
                "usage": usage,
            },
            extra_headers={"x-request-id": response_id},
        )

    def _stream_headers(self, response_id: str) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.send_header("x-request-id", response_id)
        self.end_headers()

    @staticmethod
    def _chunks(value: str) -> list[str]:
        chunk_count = max(1, int(MockConfig.stream_chunks))
        chunk_size = max(1, len(value) // chunk_count)
        return [value[index:index + chunk_size] for index in range(0, len(value), chunk_size)]

    @staticmethod
    def _sleep(delay_ms: int) -> None:
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Threaded OpenAI-compatible mock upstream for load tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18100)
    parser.add_argument("--model-name", default=MockConfig.model_name)
    parser.add_argument("--delay-ms", type=int, default=MockConfig.delay_ms)
    parser.add_argument("--stream-chunks", type=int, default=MockConfig.stream_chunks)
    parser.add_argument("--stream-chunk-delay-ms", type=int, default=MockConfig.stream_chunk_delay_ms)
    parser.add_argument("--output-chars", type=int, default=MockConfig.output_chars)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    MockConfig.model_name = args.model_name
    MockConfig.delay_ms = max(0, args.delay_ms)
    MockConfig.stream_chunks = max(1, args.stream_chunks)
    MockConfig.stream_chunk_delay_ms = max(0, args.stream_chunk_delay_ms)
    MockConfig.output_chars = max(1, args.output_chars)
    server = FastThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
