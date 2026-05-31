from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from redis.exceptions import RedisError

from app.config import get_settings
from app.database import SessionLocal
from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


logger = logging.getLogger(__name__)


class RequestLogQueueService:
    """可靠接收对外请求日志，并由后台 worker 异步落库。"""

    QUEUE_KEY = "request_logs:queue"
    PROCESSING_KEY = "request_logs:processing"
    _workers: list[asyncio.Task] = []
    _stop_event: asyncio.Event | None = None
    _runtime_enabled: bool | None = None
    _ingress_queue: asyncio.Queue[dict[str, Any]] | None = None
    _ingress_task: asyncio.Task | None = None
    _ingress_guard = threading.Lock()

    @classmethod
    def enabled(cls) -> bool:
        settings = get_settings()
        if not settings.async_request_log_enabled or not settings.redis_url.strip():
            return False
        if cls._runtime_enabled is not None:
            return cls._runtime_enabled
        return True

    @classmethod
    def enqueue(cls, **kwargs: Any) -> bool:
        if not cls.enabled():
            return False
        loop = RedisService.event_loop()
        if loop is not None:
            try:
                cls._ensure_ingress_worker(loop)
                if RedisService.event_loop_thread_id() == threading.get_ident():
                    queue = cls._ingress_queue
                    if queue is not None:
                        queue.put_nowait(dict(kwargs))
                        return True
                future = asyncio.run_coroutine_threadsafe(cls._enqueue_via_ingress(dict(kwargs)), loop)
                future.add_done_callback(lambda fut: fut.exception() if not fut.cancelled() else None)
                return True
            except asyncio.QueueFull:
                pass
            except RuntimeError:
                pass
        raw_item = dumps_json({"kwargs": kwargs})
        try:
            RedisService.get_sync_client().lpush(cls.QUEUE_KEY, raw_item)
            return True
        except (RedisError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Failed to enqueue request log; falling back to sync write: %s", exc)
            return False

    @classmethod
    def _ensure_ingress_worker(cls, loop: asyncio.AbstractEventLoop) -> None:
        with cls._ingress_guard:
            if cls._ingress_queue is None:
                ingress_queue_size = max(1000, int(getattr(get_settings(), "request_log_ingress_queue_size", 20000) or 0))
                cls._ingress_queue = asyncio.Queue(maxsize=ingress_queue_size)
            if cls._ingress_task is None or cls._ingress_task.done():
                cls._ingress_task = loop.create_task(cls._ingress_worker(), name="request-log-ingress")

    @classmethod
    async def _enqueue_via_ingress(cls, kwargs: dict[str, Any]) -> None:
        queue = cls._ingress_queue
        if queue is None:
            raise RuntimeError("request log ingress queue is not initialized")
        try:
            queue.put_nowait(kwargs)
        except asyncio.QueueFull:
            raw_item = dumps_json({"kwargs": kwargs})
            try:
                await RedisService.get_client().lpush(cls.QUEUE_KEY, raw_item)
            except (RedisError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning("Failed to enqueue request log through ingress queue; falling back to direct DB write: %s", exc)
                await asyncio.to_thread(cls._write_kwargs_batch, [kwargs])

    @classmethod
    async def _ingress_worker(cls) -> None:
        while True:
            try:
                queue = cls._ingress_queue
                if queue is None:
                    return
                first_item = await queue.get()
                batch = [first_item]
                batch_size = max(1, int(get_settings().request_log_queue_batch_size or 1))
                while len(batch) < batch_size:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                raw_items = [dumps_json({"kwargs": item}) for item in batch]
                try:
                    await RedisService.get_client().lpush(cls.QUEUE_KEY, *raw_items)
                except (RedisError, RuntimeError, TypeError, ValueError) as exc:
                    logger.warning("Failed to flush request log ingress batch to Redis; falling back to direct DB write: %s", exc)
                    await asyncio.to_thread(cls._write_kwargs_batch, batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Request log ingress worker failed: %s", exc)
                await asyncio.sleep(0.05)

    @classmethod
    async def start_background_workers(cls) -> None:
        settings = get_settings()
        worker_count = max(0, int(settings.request_log_queue_worker_count or 0))
        cls._runtime_enabled = cls._load_app_setting_enabled()
        if not cls.enabled() or worker_count <= 0 or cls._workers:
            return
        cls._stop_event = asyncio.Event()
        await cls._recover_processing_items()
        cls._workers = [
            asyncio.create_task(cls._worker(index), name=f"request-log-writer-{index}")
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
        ingress_task = cls._ingress_task
        cls._ingress_task = None
        if ingress_task is not None:
            ingress_task.cancel()
            await asyncio.gather(ingress_task, return_exceptions=True)
        pending_items: list[dict[str, Any]] = []
        if cls._ingress_queue is not None:
            while True:
                try:
                    pending_items.append(cls._ingress_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
        if pending_items:
            await asyncio.to_thread(cls._write_kwargs_batch, pending_items)
        cls._ingress_queue = None
        cls._stop_event = None
        cls._runtime_enabled = None

    @staticmethod
    def _load_app_setting_enabled() -> bool:
        try:
            from app.services.setting_service import SettingService

            return bool(getattr(SettingService.get_cached(), "async_request_logging", True))
        except Exception as exc:
            logger.warning("Failed to load async request logging setting; using env switch only: %s", exc)
            return True

    @classmethod
    async def _recover_processing_items(cls) -> None:
        try:
            client = RedisService.get_client()
            items = await client.lrange(cls.PROCESSING_KEY, 0, -1)
            if items:
                await client.rpush(cls.QUEUE_KEY, *items)
                await client.delete(cls.PROCESSING_KEY)
        except Exception as exc:
            logger.warning("Failed to recover request log processing queue: %s", exc)

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
                logger.warning("Request log worker %s failed: %s", index, exc)
                await asyncio.sleep(0.2)

    @classmethod
    async def _blocking_move_to_processing(cls, *, timeout: int) -> str | None:
        client = RedisService.get_client()
        return await client.execute_command("BRPOPLPUSH", cls.QUEUE_KEY, cls.PROCESSING_KEY, timeout)

    @classmethod
    async def _drain_available_items(cls) -> list[str]:
        settings = get_settings()
        batch_size = max(1, int(settings.request_log_queue_batch_size or 1))
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
        from app.services.log_service import LogService

        if not raw_items:
            return
        db = SessionLocal()
        finalize_jobs: list[tuple[Any, dict[str, Any]]] = []
        try:
            for raw_item in raw_items:
                payload = loads_json(raw_item, {})
                kwargs = payload.get("kwargs") if isinstance(payload, dict) else None
                if not isinstance(kwargs, dict):
                    continue
                payload_kwargs = dict(kwargs)
                payload_kwargs["auto_commit"] = False
                payload_kwargs["refresh_after_create"] = False
                payload_kwargs["flush_after_add"] = False
                payload_kwargs["enqueue_finalize"] = False
                log = LogService.create_log(db, **payload_kwargs)
                finalize_jobs.append((log, kwargs))
            if finalize_jobs:
                db.flush()
            db.commit()
            RequestLogQueueService._enqueue_finalize_jobs(finalize_jobs)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _write_kwargs_batch(items: list[dict[str, Any]]) -> None:
        from app.services.log_service import LogService

        if not items:
            return
        db = SessionLocal()
        finalize_jobs: list[tuple[Any, dict[str, Any]]] = []
        try:
            for kwargs in items:
                if not isinstance(kwargs, dict):
                    continue
                payload = dict(kwargs)
                payload["auto_commit"] = False
                payload["refresh_after_create"] = False
                payload["flush_after_add"] = False
                payload["enqueue_finalize"] = False
                log = LogService.create_log(db, **payload)
                finalize_jobs.append((log, kwargs))
            if finalize_jobs:
                db.flush()
            db.commit()
            RequestLogQueueService._enqueue_finalize_jobs(finalize_jobs)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _enqueue_finalize_jobs(finalize_jobs: list[tuple[Any, dict[str, Any]]]) -> None:
        from app.services.log_service import LogService

        for log, kwargs in finalize_jobs:
            if getattr(log, "id", None) is None:
                continue
            LogService.enqueue_finalize_for_log(
                log=log,
                model_name=kwargs.get("requested_model") or kwargs.get("model_name"),
                request_path=kwargs.get("request_path"),
                token_request_payload=kwargs.get("token_request_payload"),
                token_response_payload=kwargs.get("token_response_payload"),
                token_response_text=kwargs.get("token_response_text"),
                schedule_token_fill=bool(kwargs.get("schedule_token_fill", True)),
            )

    @classmethod
    async def queue_lengths(cls) -> dict[str, int]:
        client = RedisService.get_client()
        queued, processing = await asyncio.gather(
            client.llen(cls.QUEUE_KEY),
            client.llen(cls.PROCESSING_KEY),
        )
        return {"queued": int(queued or 0), "processing": int(processing or 0)}
