import base64
import binascii
import json
import math
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from redis import Redis

try:
    import tiktoken
except Exception:  # pragma: no cover - optional dependency during bootstrap
    tiktoken = None

from sqlalchemy import or_, select, update

from app.config import get_settings
from app.database import SessionLocal
from app.models.api_client_key import ApiClientKey
from app.models.request_log import RequestLog
from app.scheduler import scheduler
from app.services.billing_service import BillingService
from app.services.log_service import LogService
from app.utils.json_utils import safeJsonParse


class TokenUsageService:
    IMMEDIATE_DELAY_MS = 250
    BACKFILL_BATCH_SIZE = 50
    FINALIZE_DEDUPE_TTL_SECONDS = 900
    MAX_FINALIZE_ATTEMPTS = 3
    RETRY_DELAYS_SECONDS = (2, 10, 30)
    _redis_client: Redis | None = None

    @staticmethod
    def enqueue_log_finalize(
        *,
        log_id: int,
        model_name: str | None,
        request_path: str | None,
        request_payload: dict[str, Any] | None = None,
        response_payload: dict[str, Any] | None = None,
        response_text: str | None = None,
        enable_usage_fill: bool = True,
    ) -> None:
        if not request_path or request_path == "/v1/models":
            return
        if not TokenUsageService._claim_finalize_job(log_id):
            return
        scheduled = TokenUsageService._schedule_finalize_job(
            log_id=log_id,
            model_name=model_name,
            request_path=request_path,
            request_payload=request_payload,
            response_payload=response_payload,
            response_text=response_text,
            enable_usage_fill=enable_usage_fill,
            delay_ms=TokenUsageService.IMMEDIATE_DELAY_MS,
        )
        if not scheduled:
            TokenUsageService._release_finalize_job(log_id)

    @staticmethod
    def _schedule_finalize_job(
        *,
        log_id: int,
        model_name: str | None,
        request_path: str | None,
        request_payload: dict[str, Any] | None = None,
        response_payload: dict[str, Any] | None = None,
        response_text: str | None = None,
        enable_usage_fill: bool = True,
        delay_ms: int,
    ) -> bool:
        if scheduler.running:
            scheduler.add_job(
                TokenUsageService.finalize_single_log,
                "date",
                run_date=datetime.now() + timedelta(milliseconds=TokenUsageService.IMMEDIATE_DELAY_MS),
                kwargs={
                    "log_id": log_id,
                    "model_name": model_name,
                    "request_path": request_path,
                    "request_payload": request_payload,
                    "response_payload": response_payload,
                    "response_text": response_text,
                    "enable_usage_fill": enable_usage_fill,
                },
                id=f"token_usage_finalize_{log_id}",
                replace_existing=True,
                misfire_grace_time=30,
            )
            return True
        return False

    @staticmethod
    def _get_redis_client() -> Redis | None:
        if TokenUsageService._redis_client is not None:
            return TokenUsageService._redis_client
        redis_url = get_settings().redis_url.strip()
        if not redis_url:
            return None
        try:
            TokenUsageService._redis_client = Redis.from_url(redis_url, decode_responses=True)
            return TokenUsageService._redis_client
        except Exception:
            return None

    @staticmethod
    def _claim_finalize_job(log_id: int) -> bool:
        client = TokenUsageService._get_redis_client()
        if client is None:
            return True
        try:
            return bool(
                client.set(
                    f"token_usage:finalize:dedupe:{log_id}",
                    "1",
                    nx=True,
                    ex=TokenUsageService.FINALIZE_DEDUPE_TTL_SECONDS,
                )
            )
        except Exception:
            return True

    @staticmethod
    def _release_finalize_job(log_id: int) -> None:
        client = TokenUsageService._get_redis_client()
        if client is None:
            return
        try:
            client.delete(f"token_usage:finalize:dedupe:{log_id}")
        except Exception:
            return

    @staticmethod
    def finalize_single_log(
        *,
        log_id: int,
        model_name: str | None = None,
        request_path: str | None = None,
        request_payload: dict[str, Any] | None = None,
        response_payload: dict[str, Any] | None = None,
        response_text: str | None = None,
        enable_usage_fill: bool = True,
    ) -> None:
        db = SessionLocal()
        try:
            log = db.get(RequestLog, log_id)
            if log is None:
                return
            log.token_finalize_attempt_count = int(log.token_finalize_attempt_count or 0) + 1
            log.token_finalize_error = None
            db.commit()
            TokenUsageService._fill_usage_for_log(
                db,
                log,
                model_name=model_name,
                request_path=request_path,
                request_payload=request_payload,
                response_payload=response_payload,
                response_text=response_text,
                enable_usage_fill=enable_usage_fill,
            )
            TokenUsageService._release_finalize_job(log_id)
        except Exception as exc:
            db.rollback()
            TokenUsageService._record_finalize_failure(log_id=log_id, error=exc)
            TokenUsageService._schedule_retry_if_needed(
                log_id=log_id,
                model_name=model_name,
                request_path=request_path,
                request_payload=request_payload,
                response_payload=response_payload,
                response_text=response_text,
                enable_usage_fill=enable_usage_fill,
            )
        finally:
            db.close()

    @staticmethod
    def backfill_missing_usage(limit: int = BACKFILL_BATCH_SIZE) -> None:
        db = SessionLocal()
        try:
            logs = list(
                db.scalars(
                    select(RequestLog)
                    .where(RequestLog.request_path.is_not(None))
                    .where(RequestLog.request_path != "/v1/models")
                    .where(
                        or_(
                            RequestLog.prompt_tokens.is_(None),
                            RequestLog.completion_tokens.is_(None),
                            RequestLog.total_tokens.is_(None),
                            RequestLog.billing_finalized_at.is_(None),
                            RequestLog.billing_status == "pending_tokens",
                        )
                    )
                    .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                    .limit(max(1, limit))
                )
            )
            for log in logs:
                TokenUsageService.enqueue_log_finalize(
                    log_id=log.id,
                    model_name=log.requested_model or log.model_name,
                    request_path=log.request_path,
                    enable_usage_fill=tiktoken is not None,
                )
        finally:
            db.close()

    @staticmethod
    def _record_finalize_failure(*, log_id: int, error: Exception) -> None:
        db = SessionLocal()
        try:
            log = db.get(RequestLog, log_id)
            if log is None:
                return
            log.token_finalize_error = str(error)[:1000]
            log.billing_error = log.billing_error or str(error)[:1000]
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _schedule_retry_if_needed(
        *,
        log_id: int,
        model_name: str | None,
        request_path: str | None,
        request_payload: dict[str, Any] | None = None,
        response_payload: dict[str, Any] | None = None,
        response_text: str | None = None,
        enable_usage_fill: bool = True,
    ) -> None:
        db = SessionLocal()
        try:
            log = db.get(RequestLog, log_id)
            if log is None:
                return
            attempts = int(log.token_finalize_attempt_count or 0)
            if attempts >= TokenUsageService.MAX_FINALIZE_ATTEMPTS:
                TokenUsageService._release_finalize_job(log_id)
                TokenUsageService._write_finalize_alert(db, log)
                return
            delay_seconds = TokenUsageService.RETRY_DELAYS_SECONDS[min(attempts - 1, len(TokenUsageService.RETRY_DELAYS_SECONDS) - 1)]
        finally:
            db.close()
        scheduled = TokenUsageService._schedule_finalize_job(
            log_id=log_id,
            model_name=model_name,
            request_path=request_path,
            request_payload=request_payload,
            response_payload=response_payload,
            response_text=response_text,
            enable_usage_fill=enable_usage_fill,
            delay_ms=delay_seconds * 1000,
        )
        if not scheduled:
            TokenUsageService._release_finalize_job(log_id)

    @staticmethod
    def _write_finalize_alert(db, log: RequestLog) -> None:
        from app.models.alert_event import AlertEvent
        from app.utils.json_utils import dumps_json

        now = datetime.utcnow()
        alert_key = f"billing_finalize:{log.id}"
        item = db.scalar(select(AlertEvent).where(AlertEvent.alert_key == alert_key))
        if item is None:
            item = AlertEvent(
                alert_key=alert_key,
                alert_type="billing",
                first_seen_at=now,
            )
            db.add(item)
        item.severity = "warning"
        item.title = f"计费后台任务失败 · 日志 {log.id}"
        item.message = log.token_finalize_error or log.billing_error or "日志 Token/计费后台任务超过最大重试次数"
        item.payload_json = dumps_json(
            {
                "request_log_id": log.id,
                "trace_id": log.trace_id,
                "request_id": log.request_id,
                "api_client_key_id": log.api_client_key_id,
                "token_finalize_attempt_count": log.token_finalize_attempt_count,
                "billing_attempt_count": log.billing_attempt_count,
                "token_finalize_error": log.token_finalize_error,
                "billing_error": log.billing_error,
            }
        )
        item.status = "active"
        item.last_seen_at = now
        item.resolved_at = None
        db.commit()

    @staticmethod
    def _fill_usage_for_log(
        db,
        log: RequestLog,
        *,
        model_name: str | None = None,
        request_path: str | None = None,
        request_payload: dict[str, Any] | None = None,
        response_payload: dict[str, Any] | None = None,
        response_text: str | None = None,
        enable_usage_fill: bool = True,
    ) -> None:
        original_prompt_tokens = log.prompt_tokens
        original_completion_tokens = log.completion_tokens
        original_total_tokens = log.total_tokens
        original_cache_read_tokens = log.cache_read_tokens
        original_cache_write_tokens = log.cache_write_tokens
        effective_model = model_name or log.requested_model or log.model_name
        effective_path = request_path or log.request_path
        request_data = request_payload if isinstance(request_payload, dict) else TokenUsageService._parse_json_object(log.request_body_json)
        response_data = response_payload if isinstance(response_payload, dict) else TokenUsageService._parse_json_object(log.response_body_json)
        effective_response_text = response_text if isinstance(response_text, str) else log.response_text

        usage_from_upstream = TokenUsageService._extract_usage_from_response(response_data)
        prompt_tokens = log.prompt_tokens if log.prompt_tokens is not None else usage_from_upstream["prompt_tokens"]
        completion_tokens = log.completion_tokens if log.completion_tokens is not None else usage_from_upstream["completion_tokens"]
        total_tokens = log.total_tokens if log.total_tokens is not None else usage_from_upstream["total_tokens"]
        cache_read_tokens = log.cache_read_tokens if log.cache_read_tokens is not None else usage_from_upstream["cache_read_tokens"]
        cache_write_tokens = log.cache_write_tokens if log.cache_write_tokens is not None else usage_from_upstream["cache_write_tokens"]

        if enable_usage_fill and tiktoken is not None and prompt_tokens is None and request_data is not None:
            prompt_tokens = TokenUsageService._count_request_tokens(
                request_data,
                model_name=effective_model,
                request_path=effective_path,
            )

        if enable_usage_fill and tiktoken is not None and completion_tokens is None:
            response_text_value = effective_response_text or TokenUsageService._extract_response_text(response_data)
            if response_text_value:
                completion_tokens = TokenUsageService._count_text_tokens(response_text_value, effective_model)

        if enable_usage_fill and total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens

        changed = False
        if prompt_tokens is not None and log.prompt_tokens != prompt_tokens:
            log.prompt_tokens = prompt_tokens
            changed = True
        if completion_tokens is not None and log.completion_tokens != completion_tokens:
            log.completion_tokens = completion_tokens
            changed = True
        if total_tokens is not None and log.total_tokens != total_tokens:
            log.total_tokens = total_tokens
            changed = True
        if cache_read_tokens is not None and log.cache_read_tokens != cache_read_tokens:
            log.cache_read_tokens = cache_read_tokens
            changed = True
        if cache_write_tokens is not None and log.cache_write_tokens != cache_write_tokens:
            log.cache_write_tokens = cache_write_tokens
            changed = True

        if LogService.refresh_derived_fields(log, response_payload=response_data):
            changed = True

        should_finalize_billing = log.api_client_key_id is not None
        should_commit = changed
        billing_delta = None
        cache_usage_changed = (
            int(log.cache_read_tokens or 0) != int(original_cache_read_tokens or 0)
            or int(log.cache_write_tokens or 0) != int(original_cache_write_tokens or 0)
        )
        if changed or should_finalize_billing:
            TokenUsageService._sync_api_client_key_usage_delta(
                db,
                log=log,
                original_prompt_tokens=original_prompt_tokens,
                original_completion_tokens=original_completion_tokens,
                original_total_tokens=original_total_tokens,
                force=should_finalize_billing,
            )
            billing_delta = BillingService.finalize_request_log_billing(db, log)
            should_commit = True
        elif cache_usage_changed:
            billing_delta = BillingService.finalize_request_log_billing(db, log)
            should_commit = True
        if should_commit:
            db.commit()
        if billing_delta is not None:
            TokenUsageService._record_redis_usage_counters(
                log,
                original_total_tokens=original_total_tokens,
                billing_delta=billing_delta,
            )

    @staticmethod
    def _sync_api_client_key_usage_delta(
        db,
        *,
        log: RequestLog,
        original_prompt_tokens: int | None,
        original_completion_tokens: int | None,
        original_total_tokens: int | None,
        force: bool = False,
    ) -> None:
        if log.api_client_key_id is None:
            return
        prompt_delta = int(log.prompt_tokens or 0) - int(original_prompt_tokens or 0)
        completion_delta = int(log.completion_tokens or 0) - int(original_completion_tokens or 0)
        total_delta = int(log.total_tokens or 0) - int(original_total_tokens or 0)
        if not force and prompt_delta == 0 and completion_delta == 0 and total_delta == 0:
            return
        if prompt_delta == 0 and completion_delta == 0 and total_delta == 0:
            return
        db.execute(
            update(ApiClientKey)
            .where(ApiClientKey.id == log.api_client_key_id)
            .values(
                prompt_tokens_used=ApiClientKey.prompt_tokens_used + prompt_delta,
                completion_tokens_used=ApiClientKey.completion_tokens_used + completion_delta,
                total_tokens_used=ApiClientKey.total_tokens_used + total_delta,
                last_used_at=datetime.utcnow(),
            )
        )

    @staticmethod
    def _record_redis_usage_counters(
        log: RequestLog,
        *,
        original_total_tokens: int | None,
        billing_delta,
    ) -> None:
        if log.api_client_key_id is None:
            return
        client = TokenUsageService._get_redis_client()
        if client is None:
            return
        token_delta = int(log.total_tokens or 0) - int(original_total_tokens or 0)
        cost_delta = BillingService.to_float(billing_delta) or 0
        if token_delta == 0 and cost_delta == 0:
            return
        usage_time = log.created_at or datetime.utcnow()
        day_key = usage_time.strftime("%Y%m%d")
        minute_key = usage_time.strftime("%Y%m%d%H%M")
        try:
            pipe = client.pipeline(transaction=True)
            if token_delta != 0:
                pipe.incrby(f"quota:api_key:{log.api_client_key_id}:tokens:total", token_delta)
                pipe.incrby(f"quota:api_key:{log.api_client_key_id}:tokens:{day_key}", token_delta)
                pipe.expire(f"quota:api_key:{log.api_client_key_id}:tokens:{day_key}", 60 * 60 * 26)
                pipe.incrby(f"quota:api_key:{log.api_client_key_id}:tpm:{minute_key}", token_delta)
                pipe.expire(f"quota:api_key:{log.api_client_key_id}:tpm:{minute_key}", 180)
            if cost_delta != 0:
                pipe.incrbyfloat(f"quota:api_key:{log.api_client_key_id}:cost:total", cost_delta)
                pipe.incrbyfloat(f"quota:api_key:{log.api_client_key_id}:cost:{day_key}", cost_delta)
                pipe.expire(f"quota:api_key:{log.api_client_key_id}:cost:{day_key}", 60 * 60 * 26)
            owner_user_id = log.user_account_id or TokenUsageService._resolve_owner_user_id(log.api_client_key_id)
            if owner_user_id is not None:
                month_key = usage_time.strftime("%Y%m")
                if token_delta != 0:
                    pipe.incrby(f"quota:account:{owner_user_id}:tokens:total", token_delta)
                    pipe.incrby(f"quota:account:{owner_user_id}:tokens:{day_key}", token_delta)
                    pipe.expire(f"quota:account:{owner_user_id}:tokens:{day_key}", 60 * 60 * 26)
                    pipe.incrby(f"quota:account:{owner_user_id}:tokens:{month_key}", token_delta)
                    pipe.expire(f"quota:account:{owner_user_id}:tokens:{month_key}", 60 * 60 * 24 * 33)
                if cost_delta != 0:
                    pipe.incrbyfloat(f"quota:account:{owner_user_id}:cost:total", cost_delta)
                    pipe.incrbyfloat(f"quota:account:{owner_user_id}:cost:{day_key}", cost_delta)
                    pipe.expire(f"quota:account:{owner_user_id}:cost:{day_key}", 60 * 60 * 26)
                    pipe.incrbyfloat(f"quota:account:{owner_user_id}:cost:{month_key}", cost_delta)
                    pipe.expire(f"quota:account:{owner_user_id}:cost:{month_key}", 60 * 60 * 24 * 33)
            pipe.execute()
        except Exception:
            return

    @staticmethod
    def _resolve_owner_user_id(api_client_key_id: int) -> int | None:
        db = SessionLocal()
        try:
            api_key = db.get(ApiClientKey, api_client_key_id)
            return api_key.owner_user_id if api_key is not None else None
        finally:
            db.close()

    @staticmethod
    def _parse_json_object(value: str | None) -> dict[str, Any] | None:
        parsed = safeJsonParse(value) if value else None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _extract_usage_from_response(response_data: dict[str, Any] | None) -> dict[str, int | None]:
        if not isinstance(response_data, dict):
            return {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
            }
        usage = response_data.get("usage")
        if not isinstance(usage, dict):
            nested_response = response_data.get("response")
            if isinstance(nested_response, dict):
                usage = nested_response.get("usage")
        if not isinstance(usage, dict):
            return {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
            }
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        total_tokens = usage.get("total_tokens")
        cache_read_tokens, cache_write_tokens = LogService.extract_cache_tokens({"usage": usage})
        return {
            "prompt_tokens": TokenUsageService._coerce_non_negative_int(prompt_tokens),
            "completion_tokens": TokenUsageService._coerce_non_negative_int(completion_tokens),
            "total_tokens": TokenUsageService._coerce_non_negative_int(total_tokens),
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
        }

    @staticmethod
    def _coerce_non_negative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return max(0, int(value))
        return None

    @staticmethod
    def _count_request_tokens(payload: dict[str, Any], *, model_name: str | None, request_path: str | None) -> int | None:
        # Embeddings has been retired externally. Keep legacy accounting only for historical logs.
        if request_path == "/v1/embeddings":
            return TokenUsageService._count_embedding_input_tokens(payload.get("input"), model_name)
        if request_path in {"/v1/chat/completions", "/chat/completions"}:
            return TokenUsageService._count_chat_payload_tokens(payload, model_name)
        if request_path in {"/v1/responses", "/responses"}:
            return TokenUsageService._count_responses_payload_tokens(payload, model_name)
        return TokenUsageService._count_generic_payload_tokens(payload, model_name)

    @staticmethod
    def _count_chat_payload_tokens(payload: dict[str, Any], model_name: str | None) -> int:
        messages = payload.get("messages")
        total = 0
        if isinstance(messages, list):
            total += TokenUsageService._count_chat_messages(messages, model_name)

        for key in ("tools", "functions", "function_call", "tool_choice", "response_format"):
            if key in payload:
                total += TokenUsageService._count_json_tokens(payload[key], model_name)
        return total

    @staticmethod
    def _count_responses_payload_tokens(payload: dict[str, Any], model_name: str | None) -> int:
        total = 0
        if "instructions" in payload:
            total += TokenUsageService._count_text_tokens(str(payload["instructions"]), model_name)
        if "input" in payload:
            total += TokenUsageService._count_response_input_tokens(payload["input"], model_name)
        for key in ("tools", "tool_choice", "response_format"):
            if key in payload:
                total += TokenUsageService._count_json_tokens(payload[key], model_name)
        return total

    @staticmethod
    def _count_generic_payload_tokens(payload: dict[str, Any], model_name: str | None) -> int:
        material = {}
        for key, value in payload.items():
            if key in {
                "model",
                "stream",
                "max_tokens",
                "max_completion_tokens",
                "temperature",
                "top_p",
                "presence_penalty",
                "frequency_penalty",
                "n",
                "seed",
                "metadata",
                "store",
            }:
                continue
            material[key] = value
        return TokenUsageService._count_json_tokens(material, model_name)

    @staticmethod
    def _count_embedding_input_tokens(value: Any, model_name: str | None) -> int:
        if isinstance(value, str):
            return TokenUsageService._count_text_tokens(value, model_name)
        if isinstance(value, list):
            if value and all(isinstance(item, int) for item in value):
                return len(value)
            return sum(TokenUsageService._count_embedding_input_tokens(item, model_name) for item in value)
        return 0

    @staticmethod
    def _count_chat_messages(messages: list[Any], model_name: str | None) -> int:
        total = 0
        for message in messages:
            if not isinstance(message, dict):
                total += TokenUsageService._count_json_tokens(message, model_name)
                continue
            total += 3
            role = message.get("role")
            if isinstance(role, str):
                total += TokenUsageService._count_text_tokens(role, model_name)
            name = message.get("name")
            if isinstance(name, str):
                total += TokenUsageService._count_text_tokens(name, model_name) + 1
            total += TokenUsageService._count_message_content_tokens(message.get("content"), model_name)
            if message.get("tool_calls") is not None:
                total += TokenUsageService._count_json_tokens(message.get("tool_calls"), model_name)
            if message.get("function_call") is not None:
                total += TokenUsageService._count_json_tokens(message.get("function_call"), model_name)
            residual = {
                key: value
                for key, value in message.items()
                if key not in {"role", "name", "content", "tool_calls", "function_call"}
            }
            if residual:
                total += TokenUsageService._count_json_tokens(residual, model_name)
        return total + 3

    @staticmethod
    def _count_response_input_tokens(value: Any, model_name: str | None) -> int:
        if isinstance(value, str):
            return TokenUsageService._count_text_tokens(value, model_name)
        if isinstance(value, list):
            return sum(TokenUsageService._count_response_input_tokens(item, model_name) for item in value)
        if isinstance(value, dict):
            if "role" in value and "content" in value:
                return TokenUsageService._count_chat_messages([value], model_name)
            item_type = value.get("type")
            if item_type in {"text", "input_text", "output_text"} and isinstance(value.get("text"), str):
                return TokenUsageService._count_text_tokens(value["text"], model_name)
            if item_type in {"image_url", "input_image"} or "image_url" in value:
                return TokenUsageService._estimate_image_tokens(value)
            return sum(TokenUsageService._count_response_input_tokens(item, model_name) for item in value.values())
        return 0

    @staticmethod
    def _count_message_content_tokens(content: Any, model_name: str | None) -> int:
        if isinstance(content, str):
            return TokenUsageService._count_text_tokens(content, model_name)
        if isinstance(content, list):
            total = 0
            for item in content:
                if isinstance(item, str):
                    total += TokenUsageService._count_text_tokens(item, model_name)
                    continue
                if not isinstance(item, dict):
                    total += TokenUsageService._count_json_tokens(item, model_name)
                    continue
                item_type = item.get("type")
                if item_type in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
                    total += TokenUsageService._count_text_tokens(item["text"], model_name)
                    continue
                if item_type in {"image_url", "input_image"} or "image_url" in item:
                    total += TokenUsageService._estimate_image_tokens(item)
                    continue
                total += TokenUsageService._count_json_tokens(item, model_name)
            return total
        if content is None:
            return 0
        return TokenUsageService._count_json_tokens(content, model_name)

    @staticmethod
    def _count_json_tokens(value: Any, model_name: str | None) -> int:
        try:
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except TypeError:
            serialized = str(value)
        return TokenUsageService._count_text_tokens(serialized, model_name)

    @staticmethod
    def _count_text_tokens(value: str, model_name: str | None) -> int:
        if not value:
            return 0
        encoding = TokenUsageService._encoding_for_model(model_name)
        return len(encoding.encode(value))

    @staticmethod
    @lru_cache(maxsize=64)
    def _encoding_for_model(model_name: str | None):
        if tiktoken is None:
            raise RuntimeError("tiktoken is not available")
        if model_name:
            try:
                return tiktoken.encoding_for_model(model_name)
            except KeyError:
                pass
        fallback_name = "o200k_base" if TokenUsageService._prefer_o200k(model_name) else "cl100k_base"
        return tiktoken.get_encoding(fallback_name)

    @staticmethod
    def _prefer_o200k(model_name: str | None) -> bool:
        normalized = (model_name or "").lower()
        return normalized.startswith(("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4"))

    @staticmethod
    def _extract_response_text(response_data: dict[str, Any] | None) -> str | None:
        if not isinstance(response_data, dict):
            return None
        parts: list[str] = []

        def append_text(value: str | None) -> None:
            if isinstance(value, str) and value:
                parts.append(value)

        choices = response_data.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    append_text(message.get("content"))
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    append_text(delta.get("content"))

        append_text(response_data.get("output_text"))
        output = response_data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    append_text(block.get("text"))
        return "".join(parts) or None

    @staticmethod
    def _estimate_image_tokens(item: dict[str, Any]) -> int:
        detail = "auto"
        image_url_value = item.get("image_url")
        image_url = None
        if isinstance(image_url_value, dict):
            image_url = image_url_value.get("url")
            detail = str(image_url_value.get("detail") or detail).lower()
        elif isinstance(image_url_value, str):
            image_url = image_url_value
        elif isinstance(item.get("url"), str):
            image_url = item.get("url")
            detail = str(item.get("detail") or detail).lower()

        if detail == "low":
            return 85

        width, height = TokenUsageService._extract_image_dimensions(image_url)
        if width is None or height is None:
            return 765
        return TokenUsageService._estimate_high_detail_image_tokens(width, height)

    @staticmethod
    def _estimate_high_detail_image_tokens(width: int, height: int) -> int:
        if width <= 0 or height <= 0:
            return 765
        scale = min(2048 / width, 2048 / height, 1.0)
        width = max(1, int(math.ceil(width * scale)))
        height = max(1, int(math.ceil(height * scale)))
        shortest_side = min(width, height)
        if shortest_side > 768:
            upscale = 768 / shortest_side
            width = max(1, int(math.ceil(width * upscale)))
            height = max(1, int(math.ceil(height * upscale)))
        tiles = math.ceil(width / 512) * math.ceil(height / 512)
        return 85 + (tiles * 170)

    @staticmethod
    def _extract_image_dimensions(image_url: str | None) -> tuple[int | None, int | None]:
        if not image_url or not isinstance(image_url, str):
            return None, None
        if not image_url.startswith("data:"):
            parsed = urlparse(image_url)
            query_params = dict(part.split("=", 1) for part in parsed.query.split("&") if "=" in part)
            width = TokenUsageService._safe_positive_int(query_params.get("w") or query_params.get("width"))
            height = TokenUsageService._safe_positive_int(query_params.get("h") or query_params.get("height"))
            if width and height:
                return width, height
            return None, None

        try:
            header, encoded = image_url.split(",", 1)
        except ValueError:
            return None, None
        if ";base64" not in header:
            return None, None
        try:
            binary = base64.b64decode(encoded, validate=False)
        except (binascii.Error, ValueError):
            return None, None
        return TokenUsageService._extract_binary_image_dimensions(binary)

    @staticmethod
    def _extract_binary_image_dimensions(binary: bytes) -> tuple[int | None, int | None]:
        if len(binary) >= 24 and binary.startswith(b"\x89PNG\r\n\x1a\n"):
            return int.from_bytes(binary[16:20], "big"), int.from_bytes(binary[20:24], "big")
        if len(binary) >= 10 and binary[:6] in {b"GIF87a", b"GIF89a"}:
            return int.from_bytes(binary[6:8], "little"), int.from_bytes(binary[8:10], "little")
        if len(binary) >= 2 and binary[:2] == b"\xff\xd8":
            return TokenUsageService._extract_jpeg_dimensions(binary)
        return None, None

    @staticmethod
    def _extract_jpeg_dimensions(binary: bytes) -> tuple[int | None, int | None]:
        index = 2
        length = len(binary)
        while index + 9 < length:
            if binary[index] != 0xFF:
                index += 1
                continue
            marker = binary[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > length:
                break
            segment_length = int.from_bytes(binary[index:index + 2], "big")
            if segment_length < 2 or index + segment_length > length:
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if index + 7 <= length:
                    height = int.from_bytes(binary[index + 3:index + 5], "big")
                    width = int.from_bytes(binary[index + 5:index + 7], "big")
                    return width, height
                break
            index += segment_length
        return None, None

    @staticmethod
    def _safe_positive_int(value: Any) -> int | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
