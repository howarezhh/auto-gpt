from __future__ import annotations

import asyncio
import logging
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
    _workers: list[asyncio.Task] = []
    _stop_event: asyncio.Event | None = None

    @classmethod
    def enabled(cls) -> bool:
        settings = get_settings()
        return bool(settings.redis_url.strip())

    @classmethod
    def enqueue(cls, payload: dict[str, Any]) -> bool:
        if not cls.enabled() or not cls._has_active_workers():
            return False
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
        cls._workers = []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        cls._stop_event = None

    @classmethod
    async def _recover_processing_items(cls) -> None:
        try:
            client = RedisService.get_client()
            items = await client.lrange(cls.PROCESSING_KEY, 0, -1)
            if items:
                await client.rpush(cls.QUEUE_KEY, *items)
                await client.delete(cls.PROCESSING_KEY)
        except Exception as exc:
            logger.warning("Failed to recover typed logging processing queue: %s", exc)

    @classmethod
    async def _worker(cls, index: int) -> None:
        while cls._stop_event is None or not cls._stop_event.is_set():
            try:
                raw_item = await cls._blocking_move_to_processing(timeout=1)
                if not raw_item:
                    continue
                batch = [raw_item]
                batch.extend(await cls._drain_available_items())
                await asyncio.to_thread(cls._write_batch, batch)
                client = RedisService.get_client()
                for item in batch:
                    await client.lrem(cls.PROCESSING_KEY, 1, item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Typed logging worker %s failed: %s", index, exc)
                await asyncio.sleep(0.2)

    @classmethod
    async def _blocking_move_to_processing(cls, *, timeout: int) -> str | None:
        client = RedisService.get_client()
        return await client.execute_command("BRPOPLPUSH", cls.QUEUE_KEY, cls.PROCESSING_KEY, timeout)

    @classmethod
    async def _drain_available_items(cls) -> list[str]:
        settings = get_settings()
        batch_size = max(1, int(getattr(settings, "logging_event_queue_batch_size", 200) or 1))
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
        queued, processing = await asyncio.gather(
            client.llen(cls.QUEUE_KEY),
            client.llen(cls.PROCESSING_KEY),
        )
        return {"queued": int(queued or 0), "processing": int(processing or 0)}
