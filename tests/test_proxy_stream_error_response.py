import asyncio

from fastapi import HTTPException, status

from app.routers import proxy


async def _collect_bytes(stream):
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    return b"".join(chunks)


def test_stream_pre_start_error_emits_error_event_and_done(monkeypatch) -> None:
    released = []

    async def fake_release(lease):
        released.append(lease)

    monkeypatch.setattr(proxy, "_release_request_concurrency", fake_release)
    lease = object()
    exc = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "message": "所有可用提供商在 600 秒等待重试窗口内均不可用或请求失败，已停止内部重试。",
            "code": "all_providers_unavailable_after_retry",
        },
    )

    body = asyncio.run(_collect_bytes(proxy._stream_pre_start_error(exc, lease, trace_id="trace-test")))

    assert b"event: error\n" in body
    assert b"all_providers_unavailable_after_retry" in body
    assert b"trace-test" in body
    assert body.endswith(b"data: [DONE]\n\n")
    assert released == [lease]
