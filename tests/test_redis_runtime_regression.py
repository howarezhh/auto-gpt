from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.logging.queue import LoggingQueue
from app.services.api_key_auth_cache import ApiKeyAuthCache
from app.services.cache_service import CacheService
from app.services.redis_service import RedisService
from app.services.request_log_queue_service import RequestLogQueueService
from app.services.system_metrics_service import SystemMetricsService
from app.services.token_usage_service import TokenUsageService
from app.utils.json_utils import dumps_json, loads_json


def _redis_settings(**overrides):
    values = {
        "redis_url": "redis://127.0.0.1:6379/0",
        "redis_max_connections": 123,
        "redis_socket_connect_timeout_seconds": 1.5,
        "redis_socket_timeout_seconds": 2.5,
        "redis_health_check_interval_seconds": 17,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_redis_service_client_kwargs_include_pool_and_timeout_settings(monkeypatch) -> None:
    monkeypatch.setattr("app.services.redis_service.get_settings", lambda: _redis_settings())

    assert RedisService.client_kwargs() == {
        "decode_responses": True,
        "max_connections": 123,
        "socket_connect_timeout": 1.5,
        "socket_timeout": 2.5,
        "health_check_interval": 17,
    }


def test_redis_snapshot_does_not_scan_token_finalize_dedupe_keys(monkeypatch) -> None:
    scanned_patterns: list[str] = []

    class FakeRedis:
        def ping(self) -> bool:
            return True

        def mget(self, _keys):
            return [0, 0]

        def llen(self, _key):
            return 0

        def get(self, _key):
            return 0

        def scan_iter(self, *, match, count):
            scanned_patterns.append(str(match))
            return iter(())

        def close(self) -> None:
            return None

    monkeypatch.setattr("app.services.system_metrics_service.get_settings", lambda: _redis_settings())
    monkeypatch.setattr("app.services.system_metrics_service.RedisService.create_sync_client", lambda: FakeRedis())

    snapshot = SystemMetricsService._redis_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["token_finalize_backlog"] is None
    assert "token_usage:finalize:dedupe:*" not in scanned_patterns


def test_api_key_auth_cache_auxiliary_keys_have_ttl(monkeypatch) -> None:
    calls: list[tuple] = []

    class FakeRedis:
        async def setex(self, key, ttl, value):
            calls.append(("setex", key, ttl, value))

        async def delete(self, *keys):
            calls.append(("delete", keys))

        async def set(self, key, value, *, ex=None):
            calls.append(("set", key, value, ex))

        async def sadd(self, key, value):
            calls.append(("sadd", key, value))

        async def expire(self, key, ttl):
            calls.append(("expire", key, ttl))

    api_key = SimpleNamespace(
        id=7,
        name="测试 API Key",
        remark=None,
        tenant_name=None,
        project_name=None,
        app_name=None,
        environment_name=None,
        key_prefix="ak-test",
        enabled=True,
        expires_at=None,
        qps_limit=20,
        rpm_limit=20,
        prompt_tokens_used=0,
        completion_tokens_used=0,
        total_tokens_used=0,
        total_cost_used=0,
        owner_user_id=3,
        allowed_model_names_json="[]",
        allowed_endpoint_paths_json="[]",
        allowed_source_ips_json="[]",
        preferred_provider_ids_json="[]",
        preferred_region_tags_json="[]",
        latency_bias=0,
        success_rate_bias=0,
    )

    monkeypatch.setattr(
        "app.services.api_key_auth_cache.get_settings",
        lambda: SimpleNamespace(api_key_auth_cache_ttl_seconds=60),
    )
    monkeypatch.setattr("app.services.api_key_auth_cache.RedisService.get_client", lambda: FakeRedis())

    asyncio.run(
        ApiKeyAuthCache.async_set_auth_context(
            key_hash="hash-7",
            api_key=api_key,
            allowed_provider_ids=[],
            remaining_balance=None,
            policy_snapshot_json="{}",
        )
    )

    assert ("set", ApiKeyAuthCache.api_key_hash_key(7), "hash-7", 120) in calls
    assert ("expire", ApiKeyAuthCache.user_api_keys_key(3), 120) in calls


def test_shared_cache_redis_ttl_uses_configured_jitter(monkeypatch) -> None:
    calls: list[tuple] = []

    class FakeRedis:
        def get(self, key):
            calls.append(("get", key))
            return 0

        def setex(self, key, ttl, value):
            calls.append(("setex", key, ttl, value))

    monkeypatch.setattr(
        "app.services.cache_service.get_settings",
        lambda: SimpleNamespace(
            cache_l1_ttl_cap_seconds=1,
            cache_l1_max_entries=100,
            cache_redis_ttl_jitter_ratio=0.2,
        ),
    )
    monkeypatch.setattr("app.services.cache_service.uniform", lambda _start, _end: 0.1)
    monkeypatch.setattr("app.services.cache_service.RedisService.get_sync_client", lambda: FakeRedis())

    CacheService.set("route-candidates:测试模型", {"ok": True}, ttl_seconds=100)

    setex_calls = [item for item in calls if item[0] == "setex"]
    assert len(setex_calls) == 1
    assert setex_calls[0][1] == "shared-cache:route-candidates:v0:route-candidates:测试模型"
    assert setex_calls[0][2] == 110


def test_shared_cache_prefix_invalidation_uses_generation_without_scan(monkeypatch) -> None:
    calls: list[tuple] = []

    class FakeRedis:
        def incr(self, key):
            calls.append(("incr", key))

        def scan_iter(self, *, match, count):
            calls.append(("scan_iter", match, count))
            return iter(())

    monkeypatch.setattr("app.services.cache_service.RedisService.get_sync_client", lambda: FakeRedis())

    CacheService._redis_invalidate_prefix("route-candidates")

    assert calls == [("incr", "shared-cache-generation:route-candidates")]


def test_request_log_queue_requeues_failed_item_before_retry_cap(monkeypatch) -> None:
    operations: list[tuple] = []

    class FakePipeline:
        def rpush(self, key, value):
            operations.append(("rpush", key, value))
            return self

        def lpush(self, key, value):
            operations.append(("lpush", key, value))
            return self

        def ltrim(self, key, start, end):
            operations.append(("ltrim", key, start, end))
            return self

        def lrem(self, key, count, value):
            operations.append(("lrem", key, count, value))
            return self

        def incr(self, key):
            operations.append(("incr", key))
            return self

        async def execute(self):
            operations.append(("execute",))

    class FakeRedis:
        def pipeline(self, transaction=True):
            operations.append(("pipeline", transaction))
            return FakePipeline()

    monkeypatch.setattr("app.services.request_log_queue_service.RedisService.get_client", lambda: FakeRedis())
    raw_item = dumps_json({"kwargs": {"trace_id": "trace-retry"}, "_queue_attempts": 0})

    asyncio.run(RequestLogQueueService._requeue_processing_batch([raw_item], error=RuntimeError("数据库暂不可用")))

    requeued = [item for item in operations if item[0] == "rpush"]
    assert len(requeued) == 1
    retry_payload = loads_json(requeued[0][2], {})
    assert retry_payload["_queue_attempts"] == 1
    assert retry_payload["kwargs"]["trace_id"] == "trace-retry"
    assert not [item for item in operations if item[0] == "lpush" and item[1] == RequestLogQueueService.DEAD_LETTER_KEY]


def test_request_log_queue_moves_failed_item_to_dead_letter_after_retry_cap(monkeypatch) -> None:
    operations: list[tuple] = []

    class FakePipeline:
        def rpush(self, key, value):
            operations.append(("rpush", key, value))
            return self

        def lpush(self, key, value):
            operations.append(("lpush", key, value))
            return self

        def ltrim(self, key, start, end):
            operations.append(("ltrim", key, start, end))
            return self

        def lrem(self, key, count, value):
            operations.append(("lrem", key, count, value))
            return self

        def incr(self, key):
            operations.append(("incr", key))
            return self

        async def execute(self):
            operations.append(("execute",))

    class FakeRedis:
        def pipeline(self, transaction=True):
            operations.append(("pipeline", transaction))
            return FakePipeline()

    monkeypatch.setattr("app.services.request_log_queue_service.RedisService.get_client", lambda: FakeRedis())
    raw_item = dumps_json(
        {
            "kwargs": {"trace_id": "trace-dead-letter"},
            "_queue_attempts": RequestLogQueueService.MAX_PROCESSING_ATTEMPTS - 1,
        }
    )

    asyncio.run(RequestLogQueueService._requeue_processing_batch([raw_item], error=RuntimeError("字段持续异常")))

    assert not [item for item in operations if item[0] == "rpush"]
    dead_letters = [item for item in operations if item[0] == "lpush" and item[1] == RequestLogQueueService.DEAD_LETTER_KEY]
    assert len(dead_letters) == 1
    dead_letter_payload = loads_json(dead_letters[0][2], {})
    assert dead_letter_payload["attempts"] == RequestLogQueueService.MAX_PROCESSING_ATTEMPTS
    assert "字段持续异常" in dead_letter_payload["error"]
    assert ("incr", RequestLogQueueService.FAILURE_COUNT_KEY) in operations


def test_typed_logging_queue_requeues_failed_item_before_retry_cap(monkeypatch) -> None:
    operations: list[tuple] = []

    class FakePipeline:
        def rpush(self, key, value):
            operations.append(("rpush", key, value))
            return self

        def lpush(self, key, value):
            operations.append(("lpush", key, value))
            return self

        def ltrim(self, key, start, end):
            operations.append(("ltrim", key, start, end))
            return self

        def lrem(self, key, count, value):
            operations.append(("lrem", key, count, value))
            return self

        def incr(self, key):
            operations.append(("incr", key))
            return self

        async def execute(self):
            operations.append(("execute",))

    class FakeRedis:
        def pipeline(self, transaction=True):
            operations.append(("pipeline", transaction))
            return FakePipeline()

    monkeypatch.setattr("app.logging.queue.RedisService.get_client", lambda: FakeRedis())
    raw_item = dumps_json({"envelope": {}, "payload": {}, "_queue_attempts": 0})

    asyncio.run(LoggingQueue._requeue_processing_batch([raw_item], error=RuntimeError("事件表暂不可写")))

    requeued = [item for item in operations if item[0] == "rpush"]
    assert len(requeued) == 1
    retry_payload = loads_json(requeued[0][2], {})
    assert retry_payload["_queue_attempts"] == 1
    assert retry_payload["_last_queue_error"] == "事件表暂不可写"
    assert not [item for item in operations if item[0] == "lpush" and item[1] == LoggingQueue.DEAD_LETTER_KEY]


def test_typed_logging_queue_moves_failed_item_to_dead_letter_after_retry_cap(monkeypatch) -> None:
    operations: list[tuple] = []

    class FakePipeline:
        def rpush(self, key, value):
            operations.append(("rpush", key, value))
            return self

        def lpush(self, key, value):
            operations.append(("lpush", key, value))
            return self

        def ltrim(self, key, start, end):
            operations.append(("ltrim", key, start, end))
            return self

        def lrem(self, key, count, value):
            operations.append(("lrem", key, count, value))
            return self

        def incr(self, key):
            operations.append(("incr", key))
            return self

        async def execute(self):
            operations.append(("execute",))

    class FakeRedis:
        def pipeline(self, transaction=True):
            operations.append(("pipeline", transaction))
            return FakePipeline()

    monkeypatch.setattr("app.logging.queue.RedisService.get_client", lambda: FakeRedis())
    raw_item = dumps_json(
        {
            "envelope": {},
            "payload": {},
            "_queue_attempts": LoggingQueue.MAX_PROCESSING_ATTEMPTS - 1,
        }
    )

    asyncio.run(LoggingQueue._requeue_processing_batch([raw_item], error=RuntimeError("事件结构持续异常")))

    assert not [item for item in operations if item[0] == "rpush"]
    dead_letters = [item for item in operations if item[0] == "lpush" and item[1] == LoggingQueue.DEAD_LETTER_KEY]
    assert len(dead_letters) == 1
    dead_letter_payload = loads_json(dead_letters[0][2], {})
    assert dead_letter_payload["attempts"] == LoggingQueue.MAX_PROCESSING_ATTEMPTS
    assert "事件结构持续异常" in dead_letter_payload["error"]
    assert ("incr", LoggingQueue.FAILURE_COUNT_KEY) in operations


def test_token_finalize_max_attempts_records_dead_letter_and_alert(monkeypatch) -> None:
    calls: list[tuple] = []
    log = SimpleNamespace(
        id=99,
        token_finalize_attempt_count=TokenUsageService.MAX_FINALIZE_ATTEMPTS,
    )

    class FakeDb:
        def get(self, _model, log_id):
            calls.append(("get", log_id))
            return log

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr("app.services.token_usage_service.SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(
        TokenUsageService,
        "_record_finalize_dead_letter",
        staticmethod(lambda item: calls.append(("dead_letter", item.id))),
    )
    monkeypatch.setattr(
        TokenUsageService,
        "_release_finalize_job",
        staticmethod(lambda log_id: calls.append(("release", log_id))),
    )
    monkeypatch.setattr(
        TokenUsageService,
        "_write_finalize_alert",
        staticmethod(lambda _db, item: calls.append(("alert", item.id))),
    )

    TokenUsageService._schedule_retry_if_needed(
        log_id=99,
        model_name="测试模型",
        request_path="/v1/chat/completions",
    )

    assert ("dead_letter", 99) in calls
    assert ("release", 99) in calls
    assert ("alert", 99) in calls


def test_token_finalize_dead_letter_writes_redis_payload(monkeypatch) -> None:
    operations: list[tuple] = []

    class FakePipeline:
        def lpush(self, key, value):
            operations.append(("lpush", key, value))
            return self

        def ltrim(self, key, start, end):
            operations.append(("ltrim", key, start, end))
            return self

        def incr(self, key):
            operations.append(("incr", key))
            return self

        def execute(self):
            operations.append(("execute",))

    class FakeRedis:
        def pipeline(self, transaction=True):
            operations.append(("pipeline", transaction))
            return FakePipeline()

    log = SimpleNamespace(
        id=88,
        trace_id="trace-token-dead-letter",
        request_id="req-88",
        api_client_key_id=7,
        user_account_id=3,
        token_finalize_attempt_count=TokenUsageService.MAX_FINALIZE_ATTEMPTS,
        billing_attempt_count=2,
        billing_status="failed",
        token_finalize_error="Token 解析失败",
        billing_error="计费失败",
    )

    monkeypatch.setattr(TokenUsageService, "_get_redis_client", staticmethod(lambda: FakeRedis()))

    TokenUsageService._record_finalize_dead_letter(log)

    dead_letters = [item for item in operations if item[0] == "lpush"]
    assert len(dead_letters) == 1
    assert dead_letters[0][1] == TokenUsageService.FINALIZE_DEAD_LETTER_KEY
    payload = loads_json(dead_letters[0][2], {})
    assert payload["request_log_id"] == 88
    assert payload["attempts"] == TokenUsageService.MAX_FINALIZE_ATTEMPTS
    assert payload["token_finalize_error"] == "Token 解析失败"
    assert ("incr", TokenUsageService.FINALIZE_FAILURE_COUNT_KEY) in operations
