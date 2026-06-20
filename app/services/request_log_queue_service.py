from __future__ import annotations

import asyncio
import logging
import time
import threading
import hashlib
from datetime import datetime
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.config import get_settings
from app.database import SessionLocal
from app.services.proxy_request_context import get_current_ip_management_event_id, get_current_request_headers_json
from app.services.redis_service import RedisService
from app.utils.json_utils import dumps_json, loads_json


logger = logging.getLogger(__name__)


class RequestLogQueueService:
    """可靠接收对外请求日志，并由后台 worker 异步落库。"""

    QUEUE_KEY = "request_logs:queue"
    PROCESSING_KEY = "request_logs:processing"
    DEAD_LETTER_KEY = "request_logs:dead_letter"
    FAILURE_COUNT_KEY = "request_logs:failure_count"
    RECOVERY_BATCH_SIZE = 500
    RECOVERY_MAX_BATCHES = 100
    MAX_BATCH_SIZE = 500
    MAX_INGRESS_QUEUE_SIZE = 100000
    MAX_PROCESSING_ATTEMPTS = 5
    DEAD_LETTER_LIMIT = 1000
    DEAD_LETTER_ITEM_MAX_CHARS = 65536
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
        kwargs = cls._with_dedupe_key(dict(kwargs))
        kwargs.setdefault("ip_management_event_id", get_current_ip_management_event_id())
        kwargs.setdefault("request_headers_json", get_current_request_headers_json())
        kwargs.setdefault("_queue_enqueued_at", time.time())
        loop = RedisService.event_loop()
        if loop is not None:
            try:
                cls._ensure_ingress_worker(loop)
                if RedisService.event_loop_thread_id() == threading.get_ident():
                    queue = cls._ingress_queue
                    if queue is not None:
                        try:
                            queue.put_nowait(dict(kwargs))
                            return True
                        except asyncio.QueueFull:
                            raw_item = dumps_json({"kwargs": kwargs})
                            RedisService.get_sync_client().lpush(cls.QUEUE_KEY, raw_item)
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
                ingress_queue_size = min(
                    cls.MAX_INGRESS_QUEUE_SIZE,
                    max(1000, int(getattr(get_settings(), "request_log_ingress_queue_size", 20000) or 0)),
                )
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
        batch: list[dict[str, Any]] = []
        while True:
            try:
                queue = cls._ingress_queue
                if queue is None:
                    return
                first_item = await queue.get()
                batch = [first_item]
                batch_size = min(cls.MAX_BATCH_SIZE, max(1, int(get_settings().request_log_queue_batch_size or 1)))
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
                batch = []
            except asyncio.CancelledError:
                if batch:
                    await asyncio.to_thread(cls._write_kwargs_batch, batch)
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
                    "Request log processing recovery reached batch cap: %s",
                    cls.RECOVERY_MAX_BATCHES,
                )
        except Exception as exc:
            logger.warning("Failed to recover request log processing queue: %s", exc)

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
                logger.warning("Request log worker %s failed: %s", index, exc)
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
        batch_size = min(cls.MAX_BATCH_SIZE, max(1, int(settings.request_log_queue_batch_size or 1)))
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
            logger.warning("Failed to requeue request log processing batch: %s", exc)

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
                error=error_message or "request log queue payload is not a JSON object",
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
        payloads: list[dict[str, Any]] = []
        for raw_item in raw_items:
            payload = loads_json(raw_item, {})
            kwargs = payload.get("kwargs") if isinstance(payload, dict) else None
            if isinstance(kwargs, dict):
                payloads.append(dict(kwargs))
        RequestLogQueueService._write_payloads_batch(payloads)

    @staticmethod
    def _write_kwargs_batch(items: list[dict[str, Any]]) -> None:
        RequestLogQueueService._write_payloads_batch([dict(item) for item in items if isinstance(item, dict)])

    @staticmethod
    def _write_payloads_batch(items: list[dict[str, Any]]) -> None:
        from app.services.log_service import LogService
        from app.models.provider import Provider

        if not items:
            return
        db = SessionLocal()
        finalize_jobs: list[tuple[Any, dict[str, Any]]] = []
        created_logs: list[Any] = []
        updated_logs: list[Any] = []
        try:
            existing_by_signature = RequestLogQueueService._load_existing_logs_for_payloads(db, items)
            valid_provider_ids = RequestLogQueueService._load_valid_provider_ids(db, items, Provider)
            seen_signatures: set[tuple[Any, ...]] = set()
            for kwargs in items:
                payload = dict(kwargs)
                signature = RequestLogQueueService._dedupe_signature(payload)
                if signature is not None and signature in seen_signatures:
                    continue
                existing = existing_by_signature.get(signature) if signature is not None else None
                if existing is not None:
                    seen_signatures.add(signature)
                    RequestLogQueueService._sanitize_log_provider_id(payload, valid_provider_ids)
                    payload.pop("_dedupe_key", None)
                    payload.pop("_queue_enqueued_at", None)
                    LogService._apply_log_update_payload(existing, payload)
                    updated_logs.append(existing)
                    finalize_jobs.append((existing, payload))
                    continue
                RequestLogQueueService._sanitize_log_provider_id(payload, valid_provider_ids)
                payload.pop("_dedupe_key", None)
                payload.pop("_queue_enqueued_at", None)
                payload["auto_commit"] = False
                payload["refresh_after_create"] = False
                payload["flush_after_add"] = False
                payload["enqueue_finalize"] = False
                log = LogService.create_log(db, **payload)
                if signature is not None:
                    seen_signatures.add(signature)
                    existing_by_signature[signature] = log
                created_logs.append(log)
                finalize_jobs.append((log, kwargs))
            if finalize_jobs:
                db.flush()
                from app.logging.adapters.request_adapter import RequestLogRecorder
                from app.services.ip_management_event_service import IpManagementEventService

                for log in created_logs + updated_logs:
                    LogService.finalize_billing_if_exact_usage(db, log)
                for log in created_logs:
                    RequestLogRecorder.record_events_from_summary(db, log, auto_commit=False)
                for log in updated_logs:
                    RequestLogRecorder.record_missing_events_from_summary(db, log, auto_commit=False)
                for log, kwargs in finalize_jobs:
                    IpManagementEventService.attach_request_context(
                        db,
                        event_id=kwargs.get("ip_management_event_id"),
                        request_log_id=getattr(log, "id", None),
                        api_client_key_id=kwargs.get("api_client_key_id"),
                        api_client_key_prefix=kwargs.get("api_client_key_prefix"),
                        user_account_id=kwargs.get("user_account_id"),
                    )
            db.commit()
            RequestLogQueueService._enqueue_finalize_jobs(finalize_jobs)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _load_existing_logs_for_payloads(db, payloads: list[dict[str, Any]]) -> dict[tuple[Any, ...], Any]:
        from app.models.request_log import RequestLog

        trace_ids = {
            str(payload.get("trace_id"))
            for payload in payloads
            if payload.get("trace_id")
        }
        if not trace_ids:
            return {}
        rows = list(
            db.scalars(
                select(RequestLog)
                .options(
                    load_only(
                        RequestLog.id,
                        RequestLog.trace_id,
                        RequestLog.request_id,
                        RequestLog.log_type,
                        RequestLog.request_path,
                        RequestLog.http_method,
                    )
                )
                .where(RequestLog.trace_id.in_(trace_ids))
                .order_by(RequestLog.id.desc())
            )
        )
        existing: dict[tuple[Any, ...], Any] = {}
        for row in rows:
            signature = RequestLogQueueService._dedupe_signature(
                {
                    "trace_id": row.trace_id,
                    "request_id": row.request_id,
                    "log_type": row.log_type,
                    "request_path": row.request_path,
                    "http_method": row.http_method,
                }
            )
            if signature is not None and signature not in existing:
                existing[signature] = row
        return existing

    @staticmethod
    def _load_valid_provider_ids(db, payloads: list[dict[str, Any]], provider_cls) -> set[int]:
        provider_ids: set[int] = set()
        for payload in payloads:
            try:
                provider_ids.add(int(payload.get("provider_id")))
            except (TypeError, ValueError):
                continue
        if not provider_ids:
            return set()
        return {
            int(item)
            for item in db.scalars(select(provider_cls.id).where(provider_cls.id.in_(provider_ids)))
        }

    @staticmethod
    def _sanitize_log_provider_id(payload: dict[str, Any], valid_provider_ids: set[int]) -> None:
        provider_id = payload.get("provider_id")
        if provider_id is None:
            return
        try:
            normalized_provider_id = int(provider_id)
        except (TypeError, ValueError):
            payload["provider_id"] = None
            return
        if normalized_provider_id not in valid_provider_ids:
            payload["provider_id"] = None
            return
        payload["provider_id"] = normalized_provider_id

    @staticmethod
    def _with_dedupe_key(kwargs: dict[str, Any]) -> dict[str, Any]:
        if kwargs.get("_dedupe_key"):
            return kwargs
        stable_parts = [
            str(kwargs.get("trace_id") or ""),
            str(kwargs.get("request_id") or ""),
            str(kwargs.get("log_type") or ""),
            str(kwargs.get("request_path") or ""),
            str(kwargs.get("http_method") or ""),
            str(kwargs.get("api_client_key_id") or ""),
            str(kwargs.get("created_at") or ""),
        ]
        kwargs["_dedupe_key"] = hashlib.sha256("|".join(stable_parts).encode("utf-8")).hexdigest()
        return kwargs

    @staticmethod
    def _dedupe_signature(payload: dict[str, Any]) -> tuple[Any, ...] | None:
        trace_id = payload.get("trace_id")
        if not trace_id:
            return None
        return (
            str(trace_id),
            payload.get("request_id") or None,
            payload.get("log_type") or None,
            payload.get("request_path") or None,
            payload.get("http_method") or None,
        )

    @staticmethod
    def _enqueue_finalize_jobs(finalize_jobs: list[tuple[Any, dict[str, Any]]]) -> None:
        from app.services.log_service import LogService

        for log, kwargs in finalize_jobs:
            if getattr(log, "id", None) is None:
                continue
            LogService.enqueue_finalize_for_log(
                log=log,
                model_name=kwargs.get("model_name") or kwargs.get("requested_model"),
                request_path=kwargs.get("request_path"),
                token_request_payload=kwargs.get("token_request_payload"),
                token_response_payload=kwargs.get("token_response_payload"),
                token_response_text=kwargs.get("token_response_text"),
                schedule_token_fill=bool(kwargs.get("schedule_token_fill", True)),
            )

    @classmethod
    async def queue_lengths(cls) -> dict[str, int]:
        client = RedisService.get_client()
        queued, processing, dead_letter = await asyncio.gather(
            client.llen(cls.QUEUE_KEY),
            client.llen(cls.PROCESSING_KEY),
            client.llen(cls.DEAD_LETTER_KEY),
        )
        failure_count = await client.get(cls.FAILURE_COUNT_KEY)
        ingress = cls._ingress_queue.qsize() if cls._ingress_queue is not None else 0
        return {
            "queued": int(queued or 0),
            "processing": int(processing or 0),
            "dead_letter": int(dead_letter or 0),
            "failure_count": int(failure_count or 0),
            "ingress": int(ingress or 0),
        }

    @classmethod
    async def wait_until_idle(cls, *, timeout_seconds: float = 2.0, poll_interval_seconds: float = 0.05) -> dict[str, Any]:
        """等待请求日志队列进入空闲态，供日志页手动刷新时尽量看到最新结果。"""
        if not cls.enabled():
            return {
                "idle": True,
                "timed_out": False,
                "queued": 0,
                "processing": 0,
                "dead_letter": 0,
                "failure_count": 0,
                "ingress": 0,
            }
        if timeout_seconds <= 0:
            lengths = await cls.queue_lengths()
            return {"idle": (lengths["ingress"] + lengths["queued"] + lengths["processing"]) == 0, "timed_out": False, **lengths}
        deadline = time.monotonic() + timeout_seconds
        last_lengths = {"queued": 0, "processing": 0}
        while True:
            last_lengths = await cls.queue_lengths()
            if (last_lengths["ingress"] + last_lengths["queued"] + last_lengths["processing"]) == 0:
                return {"idle": True, "timed_out": False, **last_lengths}
            if time.monotonic() >= deadline:
                return {"idle": False, "timed_out": True, **last_lengths}
            await asyncio.sleep(max(0.01, poll_interval_seconds))

    @classmethod
    def discard_pending(cls) -> dict[str, int]:
        """清空尚未落库的请求日志队列，供管理员执行日志清空时避免旧日志回流。"""
        discarded = {"ingress": 0, "queued": 0, "processing": 0, "dead_letter": 0, "failure_count": 0}
        queue = cls._ingress_queue
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                    discarded["ingress"] += 1
                except asyncio.QueueEmpty:
                    break
        if not cls.enabled():
            return discarded
        try:
            client = RedisService.get_sync_client()
            discarded["queued"] = int(client.llen(cls.QUEUE_KEY) or 0)
            discarded["processing"] = int(client.llen(cls.PROCESSING_KEY) or 0)
            discarded["dead_letter"] = int(client.llen(cls.DEAD_LETTER_KEY) or 0)
            discarded["failure_count"] = int(client.get(cls.FAILURE_COUNT_KEY) or 0)
            client.delete(cls.QUEUE_KEY, cls.PROCESSING_KEY, cls.DEAD_LETTER_KEY, cls.FAILURE_COUNT_KEY)
        except (RedisError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Failed to discard pending request log queue before clearing logs: %s", exc)
        return discarded

from app.utils.timezone import now_beijing_aware
