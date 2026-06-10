from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestBodyReadTimeout(Exception):
    """Raised when a client sends the request body too slowly."""

    bytes_read: int
    timeout_seconds: float
    timeout_kind: str


@dataclass(frozen=True)
class RequestBodyTooLarge(Exception):
    """Raised when a request body exceeds the configured byte limit."""

    bytes_read: int
    max_bytes: int


async def read_limited_request_body(
    stream: AsyncIterator[bytes],
    *,
    max_bytes: int,
    total_timeout_seconds: float,
    idle_timeout_seconds: float,
) -> bytes:
    """Read a request body with byte, total-time and idle-time limits."""
    body = bytearray()
    started_at = time.monotonic()
    iterator = stream.__aiter__()
    while True:
        timeout = _next_timeout(
            started_at=started_at,
            total_timeout_seconds=total_timeout_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
        )
        try:
            if timeout is None:
                chunk = await iterator.__anext__()
            else:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError as exc:
            kind = "total" if _total_timeout_exceeded(started_at, total_timeout_seconds) else "idle"
            raise RequestBodyReadTimeout(
                bytes_read=len(body),
                timeout_seconds=total_timeout_seconds if kind == "total" else idle_timeout_seconds,
                timeout_kind=kind,
            ) from exc
        if not chunk:
            continue
        if max_bytes > 0 and len(body) + len(chunk) > max_bytes:
            raise RequestBodyTooLarge(bytes_read=len(body), max_bytes=max_bytes)
        body.extend(chunk)
    return bytes(body)


def _next_timeout(
    *,
    started_at: float,
    total_timeout_seconds: float,
    idle_timeout_seconds: float,
) -> float | None:
    timeouts: list[float] = []
    if total_timeout_seconds > 0:
        remaining = total_timeout_seconds - (time.monotonic() - started_at)
        if remaining <= 0:
            return 0.001
        timeouts.append(remaining)
    if idle_timeout_seconds > 0:
        timeouts.append(idle_timeout_seconds)
    if not timeouts:
        return None
    return max(0.001, min(timeouts))


def _total_timeout_exceeded(started_at: float, total_timeout_seconds: float) -> bool:
    return total_timeout_seconds > 0 and time.monotonic() - started_at >= total_timeout_seconds
