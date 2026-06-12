from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from redis.exceptions import RedisError

from app.config import get_settings
from app.database import SessionLocal
from app.logging.envelope import LogEnvelope, LogEvent
from app.logging.sinks import DatabaseLogSink
from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


logger = logging.getLogger(__name__)


class LoggingQueue:
    QUEUE_KEY = "logging_events:queue"
    PROCESSING_KEY = "logging_events:processing"
    DEAD_LETTER_KEY = "logging_events:dead_letter"
    FAILURE_COUNT_KEY = "logging_events:failure_count"
    RECOVERY_BATCH_SIZE = 500
    RECOVERY_MAX_BATCHES = 100
    MAX_BATCH_SIZE = 1000
    MAX_PROCESSING_ATTEMPTS = 5
    DEAD_LETTER_LIMIT = 1000
    DEAD_LETTER_ITEM_MAX_CHARS = 65536
    _workers: list[asyncio.Task] = []
    _stop_event: asyncio.Event | None = None

    @classmethod
    def enabled(cls) -> bool:
        settings = get_settings()
        return bool(settings.redis_url.strip())

    @classmethod
    def enqueue(cls, payload: dict[str, Any]) -> bool:
        if not cls.enabled():
            return False
        settings = get_settings()
        if bool(getattr(settings, "logging_event_queue_require_local_worker", False)) and not cls._has_active_workers():
            return False
        payload = dict(payload)
        payload.setdefault("_queue_enqueued_at", time.time())
        try:
            RedisService.get_sync_client().lpush(cls.QUEUE_KEY, dumps_json(payload))
            return True
        except (RedisError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Failed to enqueue typed log event; falling back to sync write: %s", exc)
            return False

    @classmethod
    def _has_active_workers(cls) -> bool:
        return any(not worker.done() for worker in cls._workers)

    @classmethod
    async def start_background_workers(cls) -> None:
        settings = get_settings()
        worker_count = max(0, int(getattr(settings, "logging_event_queue_worker_count", 1) or 0))
        if not cls.enabled() or worker_count <= 0 or cls._workers:
            return
        cls._stop_event = asyncio.Event()
        await cls._recover_processing_items()
        cls._workers = [
            asyncio.create_task(cls._worker(index), name=f"logging-event-writer-{index}")
            for index in range(worker_count)
        ]

    @classmethod
    async def stop_background_workers(cls) -> None:
        if cls._stop_event is not None:
            cls._stop_event.set()
        workers = list(cls._workers)
        if workers:
            done, pending = await asyncio.wait(workers, timeout=2)
            for worker in pending:
                worker.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        cls._workers = []
        cls._stop_event = None

    @classmethod
    async def _recover_processing_items(cls) -> None:
        try:
            client = RedisService.get_client()
            for _ in range(cls.RECOVERY_MAX_BATCHES):
                items = await client.lrange(cls.PROCESSING_KEY, 0, cls.RECOVERY_BATCH_SIZE - 1)
                if not items:
                    break
                await client.rpush(cls.QUEUE_KEY, *items)
                await client.ltrim(cls.PROCESSING_KEY, len(items), -1)
                if len(items) < cls.RECOVERY_BATCH_SIZE:
                    break
            else:
                logger.warning(
                    "Typed logging processing recovery reached batch cap: %s",
                    cls.RECOVERY_MAX_BATCHES,
                )
        except Exception as exc:
            logger.warning("Failed to recover typed logging processing queue: %s", exc)

    @classmethod
    async def _worker(cls, index: int) -> None:
        while cls._stop_event is None or not cls._stop_event.is_set():
            batch: list[str] = []
            write_completed = False
            try:
                raw_item = await cls._blocking_move_to_processing(timeout=1)
                if not raw_item:
                    continue
                batch = [raw_item]
                batch.extend(await cls._drain_available_items())
                await asyncio.to_thread(cls._write_batch, batch)
                write_completed = True
                await cls._ack_processing_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Typed logging worker %s failed: %s", index, exc)
                if batch and not write_completed:
                    await cls._requeue_processing_batch(batch, error=exc)
                await asyncio.sleep(0.2)

    @classmethod
    async def _blocking_move_to_processing(cls, *, timeout: int) -> str | None:
        client = RedisService.get_client()
        return await client.execute_command("BRPOPLPUSH", cls.QUEUE_KEY, cls.PROCESSING_KEY, timeout)

    @classmethod
    async def _drain_available_items(cls) -> list[str]:
        settings = get_settings()
        batch_size = min(cls.MAX_BATCH_SIZE, max(1, int(getattr(settings, "logging_event_queue_batch_size", 200) or 1)))
        if batch_size <= 1:
            return []
        client = RedisService.get_client()
        items: list[str] = []
        for _ in range(batch_size - 1):
            raw_item = await client.execute_command("RPOPLPUSH", cls.QUEUE_KEY, cls.PROCESSING_KEY)
            if not raw_item:
                break
            items.append(raw_item)
        return items

    @classmethod
    async def _ack_processing_batch(cls, batch: list[str]) -> None:
        if not batch:
            return
        client = RedisService.get_client()
        pipe = client.pipeline(transaction=True)
        for item in batch:
            pipe.lrem(cls.PROCESSING_KEY, 1, item)
        await pipe.execute()

    @classmethod
    async def _requeue_processing_batch(cls, batch: list[str], *, error: BaseException | None = None) -> None:
        if not batch:
            return
        try:
            client = RedisService.get_client()
            pipe = client.pipeline(transaction=True)
            for item in batch:
                retry_item, dead_letter_item = cls._prepare_failed_processing_item(item, error=error)
                if dead_letter_item is not None:
                    pipe.lpush(cls.DEAD_LETTER_KEY, dead_letter_item)
                    pipe.ltrim(cls.DEAD_LETTER_KEY, 0, cls.DEAD_LETTER_LIMIT - 1)
                elif retry_item is not None:
                    pipe.rpush(cls.QUEUE_KEY, retry_item)
                pipe.lrem(cls.PROCESSING_KEY, 1, item)
            pipe.incr(cls.FAILURE_COUNT_KEY)
            await pipe.execute()
        except Exception as exc:
            logger.warning("Failed to requeue typed logging processing batch: %s", exc)

    @classmethod
    def _prepare_failed_processing_item(
        cls,
        raw_item: str,
        *,
        error: BaseException | None = None,
    ) -> tuple[str | None, str | None]:
        payload = loads_json(raw_item, {})
        error_message = str(error or "")[:1000] or None
        if not isinstance(payload, dict):
            return None, cls._dead_letter_payload(
                raw_item=raw_item,
                attempts=cls.MAX_PROCESSING_ATTEMPTS,
                error=error_message or "typed logging queue payload is not a JSON object",
            )
        attempts = cls._coerce_attempt_count(payload.get("_queue_attempts")) + 1
        payload["_queue_attempts"] = attempts
        payload["_last_queue_error"] = error_message
        payload["_last_queue_failed_at"] = now_beijing_aware().isoformat()
        if attempts >= cls.MAX_PROCESSING_ATTEMPTS:
            return None, cls._dead_letter_payload(raw_item=dumps_json(payload), attempts=attempts, error=error_message)
        return dumps_json(payload), None

    @staticmethod
    def _coerce_attempt_count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _dead_letter_payload(cls, *, raw_item: str, attempts: int, error: str | None) -> str:
        clipped_item = raw_item[: cls.DEAD_LETTER_ITEM_MAX_CHARS]
        return dumps_json(
            {
                "failed_at": now_beijing_aware().isoformat(),
                "attempts": attempts,
                "error": error,
                "truncated": len(raw_item) > len(clipped_item),
                "item": clipped_item,
            }
        )

    @staticmethod
    def _write_batch(raw_items: list[str]) -> None:
        if not raw_items:
            return
        db = SessionLocal()
        try:
            for raw_item in raw_items:
                event = LoggingQueue._event_from_raw_item(raw_item)
                if event is None:
                    continue
                DatabaseLogSink.write(db, event, auto_commit=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _event_from_raw_item(raw_item: str) -> LogEvent | None:
        payload = loads_json(raw_item, {})
        if not isinstance(payload, dict):
            return None
        envelope_payload = payload.get("envelope")
        event_payload = payload.get("payload")
        if not isinstance(envelope_payload, dict) or not isinstance(event_payload, dict):
            return None
        allowed_envelope = {
            key: value
            for key, value in envelope_payload.items()
            if key in LogEnvelope.__dataclass_fields__
        }
        occurred_at = allowed_envelope.get("occurred_at")
        if isinstance(occurred_at, str):
            from datetime import datetime

            try:
                allowed_envelope["occurred_at"] = datetime.fromisoformat(occurred_at)
            except ValueError:
                allowed_envelope.pop("occurred_at", None)
        return LogEvent(envelope=LogEnvelope(**allowed_envelope), payload=event_payload)

    @classmethod
    async def queue_lengths(cls) -> dict[str, int]:
        client = RedisService.get_client()
        queued, processing, dead_letter = await asyncio.gather(
            client.llen(cls.QUEUE_KEY),
            client.llen(cls.PROCESSING_KEY),
            client.llen(cls.DEAD_LETTER_KEY),
        )
        failure_count = await client.get(cls.FAILURE_COUNT_KEY)
        return {
            "queued": int(queued or 0),
            "processing": int(processing or 0),
            "dead_letter": int(dead_letter or 0),
            "failure_count": int(failure_count or 0),
        }

    @classmethod
    async def wait_until_idle(
        cls,
        *,
        timeout_seconds: float = 2.0,
        poll_interval_seconds: float = 0.05,
    ) -> dict[str, Any]:
        if not cls.enabled():
            return {"idle": True, "timed_out": False, "queued": 0, "processing": 0, "dead_letter": 0, "failure_count": 0}
        if timeout_seconds <= 0:
            lengths = await cls.queue_lengths()
            return {
                "idle": (lengths["queued"] + lengths["processing"]) == 0,
                "timed_out": False,
                **lengths,
            }
        deadline = time.monotonic() + timeout_seconds
        while True:
            lengths = await cls.queue_lengths()
            if (lengths["queued"] + lengths["processing"]) == 0:
                return {"idle": True, "timed_out": False, **lengths}
            if time.monotonic() >= deadline:
                return {"idle": False, "timed_out": True, **lengths}
            await asyncio.sleep(max(0.01, poll_interval_seconds))

from app.utils.timezone import now_beijing_aware