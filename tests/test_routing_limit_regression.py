import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.services.concurrency_service import IngressConcurrencyService
from app.services.provider_capacity_service import ProviderCapacityService
from app.services.rate_limit_service import RateLimitExceededError, RateLimitService
from app.services.redis_service import RedisService


class _FakeRedis:
    def __init__(self, results=None, fail_key_prefix: str | None = None):
        self.calls = []
        self.results = list(results or [["ok", "", 1]])
        self.fail_key_prefix = fail_key_prefix

    async def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        if self.fail_key_prefix is not None:
            failing_key = next(str(item) for item in args if str(item).startswith(self.fail_key_prefix))
            return ["rate_limit_exceeded", failing_key, 20]
        return self.results.pop(0) if self.results else ["ok", "", 1]


class RoutingLimitRegressionTest(unittest.TestCase):
    def test_request_rate_limits_cover_global_api_key_and_account(self):
        fake = _FakeRedis(results=[["ok", "", 1], ["ok", "", 1]])

        with patch.object(RedisService, "get_client", return_value=fake):
            asyncio.run(
                RateLimitService.check_request_limits(
                    api_key_id=12,
                    api_key_qps_limit=20,
                    api_key_rpm_limit=20,
                    account_id=34,
                    account_qps_limit=20,
                    account_rpm_limit=20,
                    global_qps_limit=20,
                    global_rpm_limit=20,
                )
            )

        self.assertEqual(len(fake.calls), 2)
        qps_args = [str(item) for item in fake.calls[0][2]]
        rpm_args = [str(item) for item in fake.calls[1][2]]
        self.assertTrue(any(item.startswith("rate:global:qps:") for item in qps_args))
        self.assertTrue(any(item.startswith("rate:qps:12:") for item in qps_args))
        self.assertTrue(any(item.startswith("rate:account:qps:34:") for item in qps_args))
        self.assertTrue(any(item.startswith("rate:global:rpm:") for item in rpm_args))
        self.assertTrue(any(item.startswith("rate:rpm:12:") for item in rpm_args))
        self.assertTrue(any(item.startswith("rate:account:rpm:34:") for item in rpm_args))

    def test_request_rate_limit_error_uses_failing_scope_message(self):
        fake = _FakeRedis(fail_key_prefix="rate:account:qps:34:")

        with patch.object(RedisService, "get_client", return_value=fake):
            with self.assertRaises(RateLimitExceededError) as exc_info:
                asyncio.run(
                    RateLimitService.check_request_limits(
                        account_id=34,
                        account_qps_limit=20,
                    )
                )

        self.assertTrue(exc_info.exception.key.startswith("rate:account:qps:34:"))
        self.assertEqual(exc_info.exception.message, "Account QPS limit exceeded")

    def test_provider_capacity_lease_uses_dedicated_redis_namespace(self):
        fake = _FakeRedis(results=[["ok", 1, 1, 1, 1]])

        async def fake_async_redis():
            return fake

        provider = SimpleNamespace(
            id=56,
            max_active_requests=20,
            max_active_streams=10,
            max_qps=20,
            max_rpm=20,
        )

        with patch.object(ProviderCapacityService, "_async_redis", fake_async_redis):
            asyncio.run(ProviderCapacityService._async_redis_acquire(provider, is_stream=True, lease_id="lease-1"))

        args = [str(item) for item in fake.calls[0][2]]
        self.assertIn("provider_capacity:provider:56:active", args)
        self.assertIn("provider_capacity:provider:56:streams", args)
        self.assertNotIn("concurrency:provider:56:active", args)
        self.assertNotIn("concurrency:provider:56:streams", args)

    def test_ingress_concurrency_lease_uses_dedicated_namespace(self):
        fake = _FakeRedis(results=[["ok", "ingress:global:active"]])

        with patch.object(RedisService, "get_client", return_value=fake):
            lease = asyncio.run(
                IngressConcurrencyService.acquire(
                    request_id="trace-1",
                    ttl_seconds=60,
                    max_active_requests=20,
                )
            )

        args = [str(item) for item in fake.calls[0][2]]
        self.assertTrue(lease.acquired)
        self.assertIn("ingress:lease:trace-1", args)
        self.assertIn("ingress:global:active", args)
        self.assertNotIn("concurrency:global:active", args)


if __name__ == "__main__":
    unittest.main()
