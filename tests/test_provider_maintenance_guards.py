from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.schemas.content_guard import ContentGuardRunRequest
from app.services.content_trust_probe_service import ContentTrustProbeService
from app.services.health_service import HealthService


def _protocol_target(*, maintenance: bool = True) -> dict:
    return {
        "provider_id": 1,
        "provider_name": "维护提供商",
        "provider": {
            "id": 1,
            "name": "维护提供商",
            "base_url": "https://example.com/v1",
            "api_key": "sk-test",
            "provider_type": "openai_compatible",
            "protocol_type": "both",
            "timeout_ms": 30000,
            "max_retries": 0,
            "first_token_timeout_sec": 60,
            "maintenance_mode_enabled": maintenance,
            "maintenance_window": "每日 01:00-02:00 Asia/Shanghai",
        },
        "provider_model_id": 11,
        "model_name": "测试模型",
        "previous_supports_chat_completions": True,
        "previous_supports_responses": False,
        "previous_protocol_type": "chat_completions",
    }


def test_auto_endpoint_protocol_detection_skips_maintenance_provider(monkeypatch) -> None:
    async def fail_if_detected(target):
        raise AssertionError("维护模式下自动协议检测不应请求上游端点")

    class FakeDb:
        rolled_back = False

        def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr(HealthService, "_detect_endpoint_protocol_for_target", staticmethod(fail_if_detected))

    result = asyncio.run(
        HealthService._run_endpoint_protocol_detection_targets(
            FakeDb(),
            [_protocol_target(maintenance=True)],
            trigger_type="auto_provider_created",
        )
    )

    model_result = result["model_results"][0]
    assert result["success"] is False
    assert result["updated_count"] == 0
    assert result["maintenance_blocked_count"] == 1
    assert model_result["status"] == "skipped"
    assert model_result["error_code"] == "provider_maintenance_mode"
    assert "维护模式" in model_result["message"]
    assert model_result["protocol_type"] == "chat_completions"


def test_manual_endpoint_protocol_detection_allows_maintenance_provider(monkeypatch) -> None:
    calls = 0

    async def fake_detect(target):
        nonlocal calls
        calls += 1
        return {
            "provider_id": target["provider_id"],
            "provider_name": target["provider_name"],
            "provider_model_id": target["provider_model_id"],
            "model_name": target["model_name"],
            "status": "passed",
            "message": "手动检测完成",
            "updated": False,
            "update_allowed": False,
            "supports_chat_completions": target["previous_supports_chat_completions"],
            "supports_responses": target["previous_supports_responses"],
            "protocol_type": target["previous_protocol_type"],
            "protocol_label": "Chat Completions API",
            "endpoint_results": [],
            "latency_ms": 1,
        }

    class FakeDb:
        def rollback(self):
            pass

    monkeypatch.setattr(HealthService, "_detect_endpoint_protocol_for_target", staticmethod(fake_detect))

    result = asyncio.run(
        HealthService._run_endpoint_protocol_detection_targets(
            FakeDb(),
            [_protocol_target(maintenance=True)],
            trigger_type="manual_mount_matrix",
        )
    )

    assert calls == 1
    assert result["maintenance_blocked_count"] == 0
    assert result["model_results"][0]["message"] == "手动检测完成"


def test_auto_health_probe_returns_maintenance_result_without_running_phases(monkeypatch) -> None:
    async def fail_if_phase_runs(*args, **kwargs):
        raise AssertionError("维护模式下自动可用性检测不应运行上游探针阶段")

    provider = SimpleNamespace(
        id=1,
        name="维护提供商",
        maintenance_mode_enabled=True,
        maintenance_window="每日 01:00-02:00 Asia/Shanghai",
    )
    provider_model = SimpleNamespace(
        id=11,
        model_name="测试模型",
        health_status="healthy",
    )
    monkeypatch.setattr(HealthService, "_run_provider_phase_group", staticmethod(fail_if_phase_runs))

    result = asyncio.run(
        HealthService._run_provider_model_checks(
            provider,
            [provider_model],
            phase_keys={"text"},
            interactive_mode=False,
        )
    )[0]

    assert result["success"] is False
    assert result["status"] == "skipped"
    assert result["error_code"] == "provider_maintenance_mode"
    assert result["health_status"] == "healthy"
    assert "维护模式" in result["message"]


def test_auto_trust_probe_returns_maintenance_result_without_upstream(monkeypatch) -> None:
    provider = SimpleNamespace(
        id=1,
        name="维护提供商",
        maintenance_mode_enabled=True,
        maintenance_window="每日 01:00-02:00 Asia/Shanghai",
    )
    provider_model = SimpleNamespace(
        id=11,
        provider_id=1,
        model_name="测试模型",
        supports_stream=True,
    )

    def fake_resolve(db, payload):
        return provider, provider_model, {
            "type": "internal",
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider_model_id": provider_model.id,
            "model_name": provider_model.model_name,
        }

    async def fail_if_single_probe_runs(*args, **kwargs):
        raise AssertionError("维护模式下自动可信检测不应请求上游")

    monkeypatch.setattr(ContentTrustProbeService, "_resolve_probe_target", staticmethod(fake_resolve))
    monkeypatch.setattr(ContentTrustProbeService, "_resolve_endpoint_path", staticmethod(lambda provider, provider_model, payload: "/chat/completions"))
    monkeypatch.setattr(ContentTrustProbeService, "run_single_probe_with_boundary", staticmethod(fail_if_single_probe_runs))

    result = asyncio.run(
        ContentTrustProbeService.run_capability_probe(
            SimpleNamespace(),
            ContentGuardRunRequest(
                target_type="internal",
                provider_id=1,
                provider_model_id=11,
                probe_keys=["fixed_answer", "sse"],
                persist_internal_result=True,
            ),
            detection_source="automatic_trust_probe",
        )
    )

    assert result["summary"]["status"] == "skipped"
    assert result["summary"]["error_code"] == "provider_maintenance_mode"
    assert all(item["support_mode"] == "provider_maintenance_mode" for item in result["probe_results"])
    assert "维护模式" in result["summary"]["content_guard_reason"]
