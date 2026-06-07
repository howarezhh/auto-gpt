from __future__ import annotations

from pathlib import Path

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.content_guard_probe_service import ContentGuardProbeService
from app.services.content_guard_service import ContentGuardService
from app.services.error_catalog_service import ErrorCatalogService
from app.services.router_service import RoutePolicyContext, RouterService
from app.services.system_metrics_service import SystemMetricsService


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _check_content_guard_detection() -> None:
    risky = ContentGuardService.inspect_response_text(
        "回答已生成。扫码加入博彩返利群，访问 https://bad.example 获取优惠码。",
        request_payload={"messages": [{"role": "user", "content": "解释 Python 装饰器"}]},
    )
    _assert(risky.result == ContentGuardService.RESULT_BLOCK, f"推广/违规导流应被拦截：{risky}")
    _assert(risky.risk_level == "high", f"高风险导流应标记 high：{risky}")
    _assert("fraud_or_illegal_promotion" in risky.categories, f"应记录违规推广分类：{risky}")
    _assert(ContentGuardService.should_block(risky), "默认配置下高风险结果应阻断")

    provider_context = ContentGuardService.inspect_response_text(
        "本项目提供 OpenAI 兼容代理服务，用于统一管理提供商渠道和上游代理链路。",
        request_payload={"messages": [{"role": "user", "content": "说明本项目的代理链路"}]},
    )
    _assert(provider_context.result == ContentGuardService.RESULT_PASS, f"正常代理/提供商语境不应误判：{provider_context}")

    anti_fraud_context = ContentGuardService.inspect_response_text(
        "防范诈骗需要核验来源、拒绝陌生转账，并保留沟通证据。",
        request_payload={"messages": [{"role": "user", "content": "如何防范诈骗"}]},
    )
    _assert(anti_fraud_context.result == ContentGuardService.RESULT_PASS, f"正常反诈说明不应误判违规导流：{anti_fraud_context}")

    review = ContentGuardService.inspect_response_text(
        "这里有一个参考链接：https://example.com/docs",
        request_payload={"messages": [{"role": "user", "content": "写一段普通说明"}]},
    )
    _assert(review.result == ContentGuardService.RESULT_REVIEW, f"单纯异常外链应进入复核：{review}")

    allowed = ContentGuardService.inspect_response_text(
        "营销落地页可放置优惠链接：https://example.com/campaign",
        request_payload={"messages": [{"role": "user", "content": "请写广告落地页文案并包含链接"}]},
    )
    _assert(allowed.result == ContentGuardService.RESULT_PASS, f"用户明确要求广告链接时不应误拦截：{allowed}")

    key_community = ContentGuardService.inspect_response_text(
        "模型公益key即将开放，欢迎加入新社区等待通知。",
        request_payload={"messages": [{"role": "user", "content": "解释 Python 装饰器"}]},
    )
    _assert(key_community.result == ContentGuardService.RESULT_BLOCK, f"Key 社群引流应被默认规则拦截：{key_community}")
    _assert("advertising_or_promotion" in key_community.categories, f"Key 社群引流分类缺失：{key_community}")

    custom_rules = [
        {
            "id": "custom_sensitive_phrase",
            "name": "自定义敏感词",
            "category": "custom_guard",
            "enabled": True,
            "match_type": "keyword_any",
            "patterns": ["专属暗号"],
            "risk_level": "high",
            "action": "block",
            "score_delta": -30,
            "confidence": 0.95,
        }
    ]
    custom_result = ContentGuardService.inspect_response_text("这里包含专属暗号。", rules=custom_rules)
    _assert(custom_result.result == ContentGuardService.RESULT_BLOCK, f"自定义规则应参与检测：{custom_result}")
    empty_result = ContentGuardService.inspect_response_text("扫码加入", rules=[])
    _assert(empty_result.result == ContentGuardService.RESULT_PASS, f"保存空规则集时不应回退默认规则：{empty_result}")


def _check_response_structure_detection() -> None:
    chat_result = ContentGuardService.inspect_json_response(
        {"id": "chatcmpl-stage33"},
        endpoint_path="/chat/completions",
    )
    _assert(chat_result.result == ContentGuardService.RESULT_BLOCK, f"Chat 响应缺 choices 应拦截：{chat_result}")
    _assert("chat_completion_schema_violation" in chat_result.categories, f"Chat 结构分类缺失：{chat_result}")

    responses_result = ContentGuardService.inspect_json_response(
        {"id": "resp-stage33"},
        endpoint_path="/responses",
    )
    _assert(responses_result.result == ContentGuardService.RESULT_BLOCK, f"Responses 响应缺 output 应拦截：{responses_result}")
    _assert("responses_schema_violation" in responses_result.categories, f"Responses 结构分类缺失：{responses_result}")

    sse_result = ContentGuardService.inspect_sse_event("not-json", endpoint_path="/chat/completions")
    _assert(sse_result.result == ContentGuardService.RESULT_BLOCK, f"SSE 非 JSON 事件应拦截：{sse_result}")
    _assert("invalid_sse_event" in sse_result.categories, f"SSE 分类缺失：{sse_result}")

    suspicious_field = ContentGuardService.inspect_json_response(
        {"id": "chatcmpl-stage33", "choices": [], "promo": "扫码联系代理"},
        endpoint_path="/chat/completions",
        request_payload={"messages": [{"role": "user", "content": "解释装饰器"}]},
    )
    _assert(suspicious_field.result == ContentGuardService.RESULT_BLOCK, f"广告字段应拦截：{suspicious_field}")
    _assert("unexpected_advertising_field" in suspicious_field.categories, f"广告字段分类缺失：{suspicious_field}")

    json_pollution = ContentGuardService.inspect_json_response(
        {"choices": [{"message": {"content": "当然可以，结果如下：{\"ok\": true}"}}]},
        endpoint_path="/chat/completions",
        request_payload={"response_format": {"type": "json_object"}},
    )
    _assert(json_pollution.result == ContentGuardService.RESULT_BLOCK, f"JSON 模式夹带自然语言应拦截：{json_pollution}")
    _assert("structured_json_pollution" in json_pollution.categories, f"JSON 污染分类缺失：{json_pollution}")

    tool_pollution = ContentGuardService.inspect_json_response(
        {"choices": [{"message": {"tool_calls": [{"function": {"name": "query", "arguments": "请先扫码 {\"q\":\"x\"}"}}]}}]},
        endpoint_path="/chat/completions",
        request_payload={"tools": [{"type": "function", "function": {"name": "query"}}]},
    )
    _assert(tool_pollution.result == ContentGuardService.RESULT_BLOCK, f"工具参数污染应拦截：{tool_pollution}")
    _assert("tool_argument_pollution" in tool_pollution.categories, f"工具参数污染分类缺失：{tool_pollution}")


def _context(**kwargs) -> RoutePolicyContext:
    return RoutePolicyContext(
        route_mode="failover",
        default_provider_id=None,
        manual_allow_fallback=True,
        **kwargs,
    )


def _provider(**kwargs) -> Provider:
    payload = {
        "id": 3301,
        "name": "stage33-内容防护测试提供商",
        "provider_type": "openai_compatible",
        "base_url": "https://example.com/v1",
        "api_key": "upstream-secret",
        "enabled": True,
        "health_status": "healthy",
        "circuit_state": "closed",
        "trust_level": "standard",
        "content_integrity_status": "unknown",
        "content_integrity_score": 80,
        "low_trust_route_enabled": False,
    }
    payload.update(kwargs)
    return Provider(**payload)


class _FakeProviderSession:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.commit_count = 0

    def get(self, model, item_id):
        if model is Provider and item_id == self.provider.id:
            return self.provider
        return None

    def commit(self) -> None:
        self.commit_count += 1


def _check_router_content_policy() -> None:
    blocked = _provider(trust_level="blocked")
    _assert(
        RouterService._content_policy_diagnostic_reason(blocked, route_context=_context()) == "provider_trust_blocked",
        "blocked 信任等级必须硬排除",
    )

    integrity_blocked = _provider(content_integrity_status="blocked")
    _assert(
        RouterService._content_policy_diagnostic_reason(integrity_blocked, route_context=_context()) == "provider_content_integrity_blocked",
        "内容完整性 blocked 必须硬排除",
    )

    low_score = _provider(content_integrity_score=20)
    _assert(
        RouterService._content_policy_diagnostic_reason(low_score, route_context=_context()) == "provider_content_integrity_score_too_low",
        "内容完整性评分过低必须硬排除，不能仅参与降权",
    )

    standard = _provider(trust_level="standard")
    _assert(
        RouterService._content_policy_diagnostic_reason(standard, route_context=_context(require_trusted_provider=True))
        == "provider_trusted_required",
        "要求可信 provider 时 standard 不应进入候选",
    )

    low_trust = _provider(trust_level="low", low_trust_route_enabled=True)
    _assert(
        RouterService._content_policy_diagnostic_reason(low_trust, route_context=_context(allow_low_trust_providers=False))
        == "provider_low_trust_route_disabled",
        "API Key 未允许低信任时 low provider 不应进入候选",
    )
    _assert(
        RouterService._content_policy_diagnostic_reason(low_trust, route_context=_context(allow_low_trust_providers=True)) is None,
        "双侧允许低信任时不应被内容策略硬排除",
    )

    official = _provider(trust_level="official", content_integrity_status="passed")
    _assert(
        RouterService._content_policy_diagnostic_reason(official, route_context=_context(require_trusted_provider=True)) is None,
        "official provider 应满足可信路由要求",
    )


def _check_high_risk_route_detection() -> None:
    _assert(
        ContentGuardService.requires_trusted_provider(
            payload={"messages": [{"role": "user", "content": "请按 JSON 输出"}], "response_format": {"type": "json_object"}},
            endpoint_path="/chat/completions",
            has_image=False,
            require_tools=False,
        ),
        "结构化 JSON 请求必须升级为可信提供商",
    )
    _assert(
        ContentGuardService.requires_trusted_provider(
            payload={"input": [{"type": "input_file", "file_id": "file-stage33"}]},
            endpoint_path="/responses",
            has_image=False,
            require_tools=False,
        ),
        "文件类请求必须升级为可信提供商",
    )
    _assert(
        ContentGuardService.requires_trusted_provider(
            payload={"messages": [{"role": "user", "content": "长上下文" * 4000}]},
            endpoint_path="/chat/completions",
            has_image=False,
            require_tools=False,
        ),
        "长上下文请求必须升级为可信提供商",
    )


def _check_error_catalog() -> None:
    spec = ErrorCatalogService.resolve(status_code=502, detail={"code": "content_integrity_violation"})
    _assert(spec.code == "content_integrity_violation", "内容完整性错误码必须进入统一错误目录")
    error = ErrorCatalogService.build_error_object(
        status_code=502,
        detail={"code": "content_integrity_violation"},
        trace_id="stage33-trace",
    )
    _assert(error["code"] == "content_integrity_violation", f"对外错误 code 不一致：{error}")
    _assert(error["trace_id"] == "stage33-trace", "内容完整性错误必须保留 trace_id")


def _check_health_probe_guard_helpers() -> None:
    json_result = ContentGuardProbeService.inspect_probe_json_response(
        {"id": "chatcmpl-stage33"},
        provider=_provider(),
        provider_model=None,
        endpoint_path="/chat/completions",
        request_payload={"model": "stage33-model", "messages": [{"role": "user", "content": "ping"}]},
    )
    _assert(json_result.result == ContentGuardService.RESULT_BLOCK, f"健康检查 JSON 探针应识别结构污染：{json_result}")

    stream_result = ContentGuardProbeService.inspect_probe_stream_chunk(
        b"data: not-json\n\n",
        endpoint_path="/chat/completions",
    )
    _assert(stream_result.result == ContentGuardService.RESULT_BLOCK, f"健康检查流式探针应识别非法 SSE：{stream_result}")

    failure = ContentGuardProbeService.probe_failure(
        endpoint_path="/chat/completions",
        endpoint_label="chat/completions",
        support_label="原生支持 chat/completions",
        latency_ms=12,
        status_code=200,
        guard_result=stream_result,
    )
    _assert(failure["success"] is False, f"内容探针失败必须转为健康检查失败：{failure}")
    _assert(failure["retryable"] is False, f"内容完整性失败不应自动重试：{failure}")
    _assert(failure["content_guard"]["content_guard_result"] == ContentGuardService.RESULT_BLOCK, f"探针失败缺少内容检测上下文：{failure}")


def _check_content_guard_metrics_alerts() -> None:
    empty = SystemMetricsService._empty_content_guard()
    for key in (
        "block_count",
        "review_count",
        "blocked_provider_count",
        "low_trust_traffic_ratio",
        "provider_content_violation_rate",
        "high_risk_provider_counts",
    ):
        _assert(key in empty, f"内容治理空指标缺少 {key}")

    events = SystemMetricsService._build_monitoring_alert_events(
        {
            "window_minutes": 5,
            "redis": {"ok": True, "active_requests": 0, "active_streams": 0},
            "database": {"ok": True},
            "traffic": {"total_requests": 0, "status_5xx_rate": 0, "status_429": 0, "status_429_rate": 0.0},
            "background": {"pending_finalize_logs": 0, "billing_failed_logs": 0, "token_failed_logs": 0},
            "providers": [],
            "content_guard": {
                **empty,
                "high_risk_provider_counts": [
                    {
                        "provider_id": 3301,
                        "provider_name": "内容污染测试提供商",
                        "high_risk_count": 3,
                        "window_minutes": 10,
                    }
                ],
            },
        }
    )
    alert_key = "monitoring:content_guard_high_risk_provider:3301"
    _assert(alert_key in events, f"10 分钟同提供商高风险 3 次必须生成告警：{events}")
    _assert(events[alert_key]["severity"] == "danger", f"内容高风险突增告警等级应为 danger：{events[alert_key]}")
    _assert("自动隔离" in events[alert_key]["message"], f"内容高风险突增告警必须说明自动隔离：{events[alert_key]}")


def _check_content_guard_auto_isolation() -> None:
    provider = _provider(
        id=3302,
        name="stage33-自动隔离测试提供商",
        trust_level="standard",
        content_integrity_status="passed",
        content_integrity_score=75,
        circuit_state="closed",
        low_trust_route_enabled=True,
    )
    provider.provider_models = [
        ProviderModel(
            id=4401,
            provider_id=provider.id,
            model_name="stage33-auto-isolation-model",
            enabled=True,
            health_status="healthy",
            circuit_state="closed",
            content_integrity_status="passed",
        )
    ]
    events = SystemMetricsService._build_monitoring_alert_events(
        {
            "window_minutes": 5,
            "redis": {"ok": True, "active_requests": 0, "active_streams": 0},
            "database": {"ok": True},
            "traffic": {"total_requests": 0, "status_5xx_rate": 0, "status_429": 0, "status_429_rate": 0.0},
            "background": {"pending_finalize_logs": 0, "billing_failed_logs": 0, "token_failed_logs": 0},
            "providers": [],
            "content_guard": {
                **SystemMetricsService._empty_content_guard(),
                "high_risk_provider_counts": [
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "high_risk_count": 3,
                        "window_minutes": 10,
                    }
                ],
            },
        }
    )
    fake_db = _FakeProviderSession(provider)
    changed = SystemMetricsService.apply_monitoring_alert_actions(fake_db, events)
    _assert(changed is True, "10 分钟高风险 3 次必须触发自动隔离状态变更")
    _assert(fake_db.commit_count == 1, f"自动隔离应提交一次状态变更：{fake_db.commit_count}")
    _assert(provider.trust_level == "blocked", f"自动隔离必须阻断提供商信任等级：{provider.trust_level}")
    _assert(provider.content_integrity_status == "blocked", f"自动隔离必须标记内容完整性 blocked：{provider.content_integrity_status}")
    _assert(provider.circuit_state == "open", f"自动隔离必须打开提供商熔断：{provider.circuit_state}")
    _assert(provider.low_trust_route_enabled is False, "自动隔离必须关闭低信任路由入口")
    _assert(provider.content_integrity_score <= 20, f"自动隔离必须压低内容完整性分：{provider.content_integrity_score}")
    model = provider.provider_models[0]
    _assert(model.content_integrity_status == "blocked", f"自动隔离必须同步隔离挂载模型：{model.content_integrity_status}")
    _assert(model.circuit_state == "open", f"自动隔离必须同步熔断挂载模型：{model.circuit_state}")
    _assert(events["monitoring:content_guard_high_risk_provider:3302"]["payload"]["auto_isolated"] is True, "告警 payload 必须标记已自动隔离")

    changed_again = SystemMetricsService.apply_monitoring_alert_actions(fake_db, events)
    _assert(changed_again is False, "重复刷新已隔离提供商不应反复提交状态变更")


def _check_content_guard_disabled_mode() -> None:
    empty = SystemMetricsService._empty_content_guard(enabled=False)
    _assert(empty["enabled"] is False, "关闭内容治理时空指标必须显式标记 disabled")
    events = SystemMetricsService._build_monitoring_alert_events(
        {
            "window_minutes": 5,
            "redis": {"ok": True, "active_requests": 0, "active_streams": 0},
            "database": {"ok": True},
            "traffic": {"total_requests": 0, "status_5xx_rate": 0, "status_429": 0, "status_429_rate": 0.0},
            "background": {"pending_finalize_logs": 0, "billing_failed_logs": 0, "token_failed_logs": 0},
            "providers": [],
            "content_guard": {
                **empty,
                "high_risk_provider_counts": [
                    {
                        "provider_id": 3303,
                        "provider_name": "内容治理关闭测试提供商",
                        "high_risk_count": 3,
                        "window_minutes": 10,
                    }
                ],
            },
        }
    )
    _assert(
        "monitoring:content_guard_high_risk_provider:3303" not in events,
        f"内容治理关闭时不应生成自动隔离告警：{events}",
    )


def _check_frontend_and_log_wiring() -> None:
    checks = [
        ("app/templates/providers.html", [
            "provider-trust-level",
            "provider-content-integrity-status",
            "provider-low-trust-route-enabled",
        ]),
        ("app/templates/api_keys.html", [
            "api-key-trusted-providers-only",
            "api-key-allow-low-trust-providers",
            "api-key-content-guard-required",
        ]),
        ("app/templates/user_api_keys.html", [
            "user-api-key-create-trusted-providers-only",
            "user-api-key-create-allow-low-trust-providers",
            "user-api-key-create-content-guard-required",
            "低信任风险提示",
        ]),
        ("app/routers/user_portal.py", [
            "trusted_providers_only: str | None = Form(default=\"on\")",
            "allow_low_trust_providers: str | None = Form(default=None)",
            "content_guard_required: str | None = Form(default=\"on\")",
        ]),
        ("app/services/user_portal_service.py", [
            "allow_low_trust_providers=api_key.allow_low_trust_providers",
            "require_trusted_provider=api_key.trusted_providers_only",
            "content_guard_required=api_key.content_guard_required",
        ]),
        ("app/templates/logs.html", [
            "logs-provider-trust-level",
            "logs-content-guard-result",
            "logs-content-guard-risk-level",
        ]),
        ("app/templates/operations.html", [
            "operations-content-block-count",
            "operations-content-review-count",
            "operations-content-low-trust-ratio",
            "operations-content-violation-rate",
        ]),
        ("app/static/js/app.js", [
            "content_guard_enabled",
            "trusted_providers_only",
            "allow_low_trust_providers",
            "content_guard_required",
            "content_guard_result",
            "content_guard_risk_level",
            "formatContentGuardResultLabel",
            "providerTrustLevelSelect",
            "provider_trust_level",
            "content_probe_last_passed_at",
            "content_probe_last_failed_at",
            "content_probe_failure_count",
            "content_probe_results",
            "recent_content_guard_events",
            "renderContentProbeSummary",
            "renderRecentContentGuardEvents",
            "const contentGuard = metrics.content_guard || {}",
            "operations-content-block-count",
            "operations-content-violation-rate",
        ]),
        ("app/services/log_service.py", [
            "content_guard_result: str | None = None",
            "content_guard_risk_level: str | None = None",
            '"content_guard_excerpt"',
        ]),
        ("app/services/system_metrics_service.py", [
            '"content_guard": content_guard',
            "low_trust_traffic_ratio",
            "monitoring:content_guard_high_risk_provider",
        ]),
        ("app/services/health_service.py", [
            "ContentGuardProbeService",
            "content_fixed_answer",
            "content_json",
            "content_sse",
            "content_refusal",
            "content_tools",
            "ContentGuardProbeService.inspect_probe_json_response",
            "ContentGuardProbeService.inspect_probe_stream_chunk",
            "ContentGuardProbeService.apply_content_probe_health",
        ]),
        ("app/services/content_guard_probe_service.py", [
            "class ContentGuardProbeService",
            "inspect_probe_json_response",
            "inspect_probe_stream_chunk",
            "content_guard_probe_failed",
            "content_probe_results_json",
            "endpoint_results: list[dict[str, Any]] | None = None",
            "capability_key",
        ]),
        ("app/routers/logs.py", [
            "provider_trust_level: str | None = None",
            "content_guard_result: str | None = None",
            "content_guard_risk_level: str | None = None",
        ]),
        ("app/templates/base.html", [
            "?v=20260607-",
            "/content-guard",
            "内容防护",
        ]),
        ("app/templates/content_guard.html", [
            "content-guard-settings-form",
            "content-guard-probe-form",
            "content-guard-external-base-url",
            "content-guard-rules-body",
            "content-guard-inspect-form",
            "content-guard-result-body",
            "content-guard-high-risk-strategy",
            "content-guard-stream-mode",
            "content-guard-max-detection-delay-ms",
            "content-guard-async-review-enabled",
        ]),
        ("app/routers/content_guard.py", [
            'prefix="/api/content-guard"',
            '"/overview"',
            '"/settings"',
            '"/rules"',
            '"/rules/reset"',
            '"/inspect-text"',
            '"/probe"',
        ]),
        ("app/services/content_guard_module_service.py", [
            "ContentGuardModuleService",
            "build_overview",
            "update_settings",
            "update_rules",
            "inspect_text",
            "run_probe",
            "external.base_url",
        ]),
        ("app/static/js/app.js", [
            "/api/content-guard/rules",
            "/api/content-guard/rules/reset",
            "/api/content-guard/inspect-text",
            "content-guard-rules-body",
            "content-guard-inspect-form",
        ]),
        ("app/services/provider_service.py", [
            "content_probe_results_json",
            "_parse_content_probe_results",
            "_build_recent_content_guard_events",
            "recent_content_guard_events",
        ]),
    ]
    for filename, needles in checks:
        text = Path(filename).read_text(encoding="utf-8", errors="ignore")
        missing = [needle for needle in needles if needle not in text]
        _assert(not missing, f"{filename} 缺少内容治理接线：{missing}")


def main() -> None:
    _check_content_guard_detection()
    _check_response_structure_detection()
    _check_router_content_policy()
    _check_high_risk_route_detection()
    _check_error_catalog()
    _check_health_probe_guard_helpers()
    _check_content_guard_metrics_alerts()
    _check_content_guard_auto_isolation()
    _check_content_guard_disabled_mode()
    _check_frontend_and_log_wiring()
    print("stage33 content guard regression check passed")


if __name__ == "__main__":
    main()
