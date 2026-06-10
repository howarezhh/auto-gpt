from __future__ import annotations

import asyncio
from types import SimpleNamespace

import stage33_content_guard_regression_check as stage33

from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.content_guard_service import ContentGuardService


def test_stage33_content_guard_regression() -> None:
    stage33.main()


def test_responses_success_error_null_is_not_upstream_error_payload() -> None:
    result = ContentGuardService._inspect_response_structure(
        {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "error": None,
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "AOTU_CONTENT_GUARD_OK",
                        }
                    ],
                }
            ],
        },
        endpoint_path="/responses",
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_responses_meaningful_error_payload_is_blocked() -> None:
    result = ContentGuardService._inspect_response_structure(
        {
            "id": "resp_test",
            "object": "response",
            "status": "failed",
            "error": {"message": "model unavailable", "code": "model_not_found"},
            "output": [],
        },
        endpoint_path="/responses",
    )

    assert result.result == ContentGuardService.RESULT_BLOCK
    assert "upstream_error_payload" in result.categories


def test_enhanced_detection_catches_obfuscated_illegal_redirect() -> None:
    result = ContentGuardService.inspect_response_text(
        "赌 博 开 户 充 值 找 客 服",
        request_payload={"messages": [{"content": "不要广告和非法导流"}]},
        rules=[],
    )

    assert result.result == ContentGuardService.RESULT_BLOCK
    assert "illegal_content" in result.categories


def test_enhanced_detection_can_be_disabled() -> None:
    result = ContentGuardService.inspect_response_text(
        "赌 博 开 户 充 值 找 客 服",
        rules=[],
        enhanced_detection_enabled=False,
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_negated_ad_phrase_is_not_flagged() -> None:
    result = ContentGuardService.inspect_response_text(
        "这是正常的技术说明，没有推广链接。",
        request_payload={"messages": [{"content": "不要广告和非法导流"}]},
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_public_safety_anti_fraud_context_is_not_flagged() -> None:
    result = ContentGuardService.inspect_response_text(
        "防范诈骗需要核验来源、拒绝陌生转账，并保留沟通证据。",
        request_payload={"messages": [{"role": "user", "content": "如何防范诈骗"}]},
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_redirect_safety_context_does_not_trigger_ad_rules() -> None:
    result = ContentGuardService.inspect_response_text(
        "识别优惠码骗局时，不要添加微信客服，不要扫码进群，也不要点击推广链接。",
        request_payload={"messages": [{"role": "user", "content": "说明如何防范广告导流"}]},
    )

    assert result.result == ContentGuardService.RESULT_PASS


def test_chinese_sentence_period_does_not_create_fake_domain() -> None:
    assert ContentGuardService.extract_domains("不要把 API Key 写入前端代码。定期轮换密钥。") == set()


def test_positive_redirect_intent_still_triggers_ad_rules() -> None:
    result = ContentGuardService.inspect_response_text(
        "立即注册并扫码领取优惠，添加客服加入社群即可获得返佣。",
        request_payload={"messages": [{"role": "user", "content": "请回答接口使用建议，不要广告"}]},
    )

    assert result.result in {ContentGuardService.RESULT_REVIEW, ContentGuardService.RESULT_BLOCK}
    assert any(
        category in result.categories
        for category in {"advertising_or_promotion", "contact_or_offplatform_redirect", "qr_code_redirect"}
    )


def test_positive_custom_score_is_not_duplicated_by_enhanced_detection() -> None:
    result = ContentGuardService.inspect_response_text(
        "低风险证据",
        rules=[
            {
                "id": "positive_score",
                "name": "正向评分",
                "category": "custom",
                "score_delta": 20,
                "patterns": ["低风险证据"],
            }
        ],
    )

    assert result.score_delta == 20


def test_pollution_scenarios_use_single_upstream_request() -> None:
    calls = 0

    async def fake_send(provider, provider_model, *, endpoint_path, payload, endpoint_label):
        nonlocal calls
        calls += 1
        return (
            {
                "id": "resp_test",
                "object": "response",
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "[[long_context_answer]]\n"
                                    "请求进入代理后先校验密钥。\n"
                                    "再选择健康提供商。\n"
                                    "随后转发上游模型。\n"
                                    "最后记录日志和用量。\n\n"
                                    "[[tool_context_answer]]\n"
                                    "healthy 表示该检查对象当前可正常响应。\n\n"
                                    "[[markdown_answer]]\n"
                                    "- 不要把 API Key 写入前端代码。\n"
                                    "- 定期轮换并保留最小权限。\n"
                                    "- 泄露后立即禁用并重建。\n\n"
                                    "[[citation_answer]]\n"
                                    "内容完整性检测用于确认模型输出没有被异常外链、广告或引流内容污染。参考：内部策略"
                                ),
                            }
                        ],
                    }
                ],
            },
            1000,
            200,
            [{"result": "fake_trace"}],
            None,
        )

    async def run_case() -> None:
        original = ContentGuardProbeService.send_content_probe_json
        ContentGuardProbeService.send_content_probe_json = staticmethod(fake_send)
        try:
            provider = SimpleNamespace(id=1, name="测试提供商", provider_type="openai", base_url="http://example.test")
            provider_model = SimpleNamespace(id=1, model_name="测试模型", custom_model_name="test-model")
            result = await ContentGuardProbeService.probe_pollution_rules(provider, provider_model, endpoint_path="/responses")
        finally:
            ContentGuardProbeService.send_content_probe_json = original
        assert calls == 1
        assert result["success"] is True
        assert len(result.get("detections") or []) == 4

    asyncio.run(run_case())


def test_fixed_answer_and_pollution_rules_use_single_upstream_request() -> None:
    calls = 0

    async def fake_send(provider, provider_model, *, endpoint_path, payload, endpoint_label):
        nonlocal calls
        calls += 1
        return (
            {
                "id": "resp_test",
                "object": "response",
                "status": "completed",
                "error": None,
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "[[fixed_answer]]\n"
                                    "AOTU_CONTENT_GUARD_OK\n\n"
                                    "[[long_context_answer]]\n"
                                    "请求进入代理后先校验密钥。\n"
                                    "再选择健康提供商。\n"
                                    "随后转发上游模型。\n"
                                    "最后记录日志和用量。\n\n"
                                    "[[tool_context_answer]]\n"
                                    "healthy 表示该检查对象当前可正常响应。\n\n"
                                    "[[markdown_answer]]\n"
                                    "- 不要把 API Key 写入前端代码。\n"
                                    "- 定期轮换并保留最小权限。\n"
                                    "- 泄露后立即禁用并重建。\n\n"
                                    "[[citation_answer]]\n"
                                    "内容完整性检测用于确认模型输出没有被异常外链、广告或引流内容污染。参考：内部策略"
                                ),
                            }
                        ],
                    }
                ],
            },
            1000,
            200,
            [{"result": "fake_trace"}],
            None,
        )

    async def run_case() -> None:
        original = ContentGuardProbeService.send_content_probe_json
        ContentGuardProbeService.send_content_probe_json = staticmethod(fake_send)
        try:
            provider = SimpleNamespace(id=1, name="测试提供商", provider_type="openai", base_url="http://example.test")
            provider_model = SimpleNamespace(id=1, model_name="测试模型", custom_model_name="test-model")
            result = await ContentGuardProbeService.probe_fixed_answer_and_pollution_rules(
                provider,
                provider_model,
                endpoint_path="/responses",
            )
        finally:
            ContentGuardProbeService.send_content_probe_json = original
        assert calls == 1
        assert result["fixed_answer"]["success"] is True
        assert result["pollution_rules"]["success"] is True
        assert result["fixed_answer"].get("raw_provider_response")
        assert result["pollution_rules"].get("raw_provider_response")

    asyncio.run(run_case())
