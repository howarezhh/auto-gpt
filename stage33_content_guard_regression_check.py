from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from app.utils.json_utils import loads_json

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.schemas.content_guard import ContentGuardExternalTarget, ContentGuardSettingsUpdate
from app.services.content_guard_probe_service import ContentGuardProbeService
import app.services.content_guard_module_service as content_guard_module_service
from app.services.content_guard_module_service import ContentGuardModuleService
from app.services.content_guard_service import ContentGuardService
from app.services.content_runtime_guard_service import ContentRuntimeGuardService
from app.services.content_trust_probe_service import ContentTrustProbeService
from app.services.error_catalog_service import ErrorCatalogService
from app.services.health_service import HealthService
from app.services.log_service import LogService
from app.services.provider_service import ProviderService
from app.services.proxy_service import ProxyService
from app.services.router_service import RoutePolicyContext, RouterService
from app.services.system_metrics_service import SystemMetricsService
from app.schemas.provider import ProviderBase, ProviderOut, ProviderUpdate
import app.tasks as app_tasks


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

    short_link = ContentGuardService.inspect_response_text(
        "更多额度请访问 https://bit.ly/aotu-vip 领取优惠码。",
        request_payload={"messages": [{"role": "user", "content": "写一段普通说明"}]},
    )
    _assert(short_link.result == ContentGuardService.RESULT_BLOCK, f"短链导流应被拦截：{short_link}")
    _assert("unexpected_link" in short_link.categories, f"短链导流分类缺失：{short_link}")

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
    return RoutePolicyContext(**kwargs)


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

    low_trust = _provider(trust_level="low")
    _assert(
        RouterService._content_policy_diagnostic_reason(low_trust, route_context=_context()) is None,
        "全局未要求只走可信时 low provider 不应被局部开关硬排除",
    )

    official = _provider(trust_level="official", content_integrity_status="passed")
    _assert(
        RouterService._content_policy_diagnostic_reason(official, route_context=_context(require_trusted_provider=True)) is None,
        "official provider 应满足可信路由要求",
    )


def _check_runtime_guard_request_semantics() -> None:
    setting_enabled = type("Setting", (), {"content_guard_enabled": True})()
    setting_disabled = type("Setting", (), {"content_guard_enabled": False})()
    provider_disabled = _provider(content_guard_enabled=False)
    provider_enabled = _provider(content_guard_enabled=True)
    route_context = _context()

    _assert(
        ContentRuntimeGuardService.enabled_for_request(
            setting=setting_enabled,
            provider=provider_disabled,
            route_context=route_context,
        ) is False,
        "provider 关闭内容防护时不得因 API Key 局部字段强制进入检测链路",
    )
    _assert(
        ContentRuntimeGuardService.enabled_for_request(
            setting=setting_enabled,
            provider=provider_disabled,
            route_context=route_context,
        ) is False,
        "provider 关闭内容防护时必须跳过运行时检测",
    )
    _assert(
        ContentRuntimeGuardService.enabled_for_request(
            setting=setting_enabled,
            provider=provider_enabled,
            route_context=route_context,
        ) is True,
        "provider 开启且全局开启时应执行运行时检测",
    )
    _assert(
        ContentRuntimeGuardService.enabled_for_request(
            setting=setting_disabled,
            provider=provider_enabled,
            route_context=route_context,
        ) is False,
        "内容完整性全局总开关关闭时必须停用请求过程检测",
    )


def _check_low_trust_route_field_hidden() -> None:
    for schema in (ProviderBase, ProviderUpdate, ProviderOut):
        _assert(
            "low_trust_route_enabled" not in schema.model_fields,
            f"{schema.__name__} 不应继续暴露低信任放行字段",
        )


def _check_high_risk_route_detection() -> None:
    _assert(
        ContentGuardService._request_expects_json_response(
            {"messages": [{"role": "user", "content": "请按 JSON 输出"}], "response_format": {"type": "json_object"}}
        ),
        "结构化 JSON 请求必须被识别为结构化输出场景",
    )
    _assert(
        ContentGuardService._payload_contains_file_reference(
            {"input": [{"type": "input_file", "file_id": "file-stage33"}]}
        ),
        "文件类请求必须被识别为文件输入场景",
    )
    _assert(
        ContentGuardService._payload_has_long_context(
            {"messages": [{"role": "user", "content": "长上下文" * 4000}]}
        ),
        "长上下文请求必须被识别为长上下文场景",
    )
    base_context = _context()
    trusted_context = ProxyService._with_content_guard_route_policy(
        base_context,
        payload={"messages": [{"role": "user", "content": "长上下文" * 4000}]},
        endpoint_path="/chat/completions",
        has_image=False,
        require_tools=False,
    )
    _assert(trusted_context is not None and trusted_context.require_trusted_provider is True, "长上下文等高风险请求必须动态要求可信提供商")
    normal_context = ProxyService._with_content_guard_route_policy(
        base_context,
        payload={"messages": [{"role": "user", "content": "ping"}]},
        endpoint_path="/chat/completions",
        has_image=False,
        require_tools=False,
    )
    _assert(normal_context is base_context, "普通请求不应被无差别提升为只走可信提供商")


def _check_content_guard_route_cache_and_scheduler_policy() -> None:
    required_context = _context()
    optional_context = _context()
    common = {
        "model_name": "stage33-model",
        "require_vision": False,
        "require_stream": False,
        "require_tools": False,
        "require_image_generation": False,
        "require_chat_completions": True,
        "require_responses": False,
    }
    required_key = RouterService._build_candidate_cache_key(route_context=required_context, **common)
    optional_key = RouterService._build_candidate_cache_key(route_context=optional_context, **common)
    _assert(required_key == optional_key and "guard-optional" not in required_key, "候选缓存 key 不应按内容检测可选拆分")

    provider = _provider()
    blocked_model = ProviderModel(
        id=4404,
        provider_id=provider.id,
        model_name="stage33-blocked-content-model",
        enabled=True,
        content_integrity_status="blocked",
        content_probe_last_failed_at=datetime.utcnow() - timedelta(minutes=30),
    )
    setting = type("Setting", (), {"content_guard_probe_interval_sec": 300})()
    _assert(
        HealthService._should_run_scheduled_content_probe(provider, blocked_model, setting=setting) is False,
        "自动预检不得每轮持续探测已隔离 blocked 模型",
    )
    unknown_model = ProviderModel(
        id=4405,
        provider_id=provider.id,
        model_name="stage33-unknown-content-model",
        enabled=True,
        content_integrity_status="unknown",
    )
    _assert(
        HealthService._should_run_scheduled_content_probe(provider, unknown_model, setting=setting) is True,
        "尚未检测模型仍应进入自动预检",
    )

    original_get_cached = app_tasks.SettingService.get_cached
    try:
        app_tasks.SettingService.get_cached = staticmethod(lambda: type("Setting", (), {"content_guard_probe_interval_sec": 900})())
        _assert(app_tasks._content_integrity_lock_ttl_seconds() == 1800, "内容预检任务锁 TTL 必须随检测间隔动态计算")
    finally:
        app_tasks.SettingService.get_cached = original_get_cached


def _check_content_guard_frontend_policy_wiring() -> None:
    app_js = Path("app/static/js/app.js").read_text(encoding="utf-8")
    api_keys_template = Path("app/templates/api_keys.html").read_text(encoding="utf-8")
    user_template = Path("app/templates/user_api_keys.html").read_text(encoding="utf-8")
    user_router = Path("app/routers/user_portal.py").read_text(encoding="utf-8")
    _assert("selectedProviderId = providerSelect.value" in app_js, "内容防护概览刷新必须保留提供商选择")
    _assert("selectedModelId = providerModelSelect.value" in app_js, "内容防护概览刷新必须保留模型选择")
    _assert("existingChecked" in app_js and "selectedKeys" in app_js, "内容防护概览刷新必须保留探针勾选状态")
    _assert("api-key-content-guard-filter" not in api_keys_template, "API Key 管理端禁止继续暴露内容检测局部筛选")
    _assert("api-key-template-content-guard-required" not in api_keys_template, "API Key 策略模板禁止继续暴露内容检测局部控件")
    _assert("content_guard_required: templateContentGuardRequiredInput.checked" not in app_js, "策略模板提交禁止继续写入内容检测局部字段")
    _assert("contentGuardRequiredInput.checked = template.content_guard_required" not in app_js, "套用模板禁止继续预填内容检测局部字段")
    _assert("user-api-key-create-content-guard-required" not in user_template, "普通用户端禁止提供关闭内容检测的创建控件")
    _assert('name="content_guard_required"' not in user_template, "普通用户端禁止提交内容检测开关字段")
    _assert("content_guard_required: str | None = Form" not in user_router, "普通用户端接口禁止接收内容检测开关字段")
    _assert('"content_guard_required": True' not in user_router, "普通用户创建/更新 API Key 禁止继续写入内容检测局部字段")


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

    trust_decision = ContentGuardProbeService._content_probe_decision(
        [
            {"phase_key": "content_fixed_answer", "success": True, "content_guard_result": "pass"},
            {"phase_key": "content_pollution_rules", "success": True, "content_guard_result": "pass"},
            {"phase_key": "content_sse", "success": True, "content_guard_result": "pass"},
        ],
        {"content_guard_result": "pass"},
    )
    _assert(trust_decision["content_guard_result"] == "pass", f"必需可信探针全部通过才应标记可信：{trust_decision}")
    incomplete_decision = ContentGuardProbeService._content_probe_decision(
        [
            {"phase_key": "content_fixed_answer", "success": True, "content_guard_result": "pass"},
        ],
        {"content_guard_result": "pass"},
    )
    _assert(incomplete_decision["content_guard_result"] != "pass", f"缺少短链/外链/广告识别探针不应标记可信：{incomplete_decision}")


def _check_content_probe_health_window_and_recovery() -> None:
    provider = _provider(
        id=3303,
        name="stage33-探针窗口测试提供商",
        content_integrity_status="blocked",
        circuit_state="open",
        trust_level="blocked",
    )
    provider_model = ProviderModel(
        id=4403,
        provider_id=provider.id,
        model_name="stage33-probe-window-model",
        enabled=True,
        health_status="healthy",
        circuit_state="open",
        content_integrity_status="blocked",
        content_probe_failure_count=2,
        content_probe_last_failed_at=datetime.utcnow() - timedelta(minutes=11),
    )
    provider.provider_models = [provider_model]
    db = _ProbeHealthSession()
    review_result = {
        "content_guard_result": ContentGuardService.RESULT_REVIEW,
        "content_guard_reason": "临时探针失败",
        "content_guard_action": "record",
    }
    endpoint_results = [
        {
            "capability_key": "content_fixed_answer",
            "endpoint_label": "固定答案完整性探针",
            "success": False,
            "content_guard": review_result,
        }
    ]
    ContentGuardProbeService.apply_content_probe_health(
        db,
        provider,
        provider_model,
        content_guard_result=review_result,
        endpoint_results=endpoint_results,
    )
    _assert(provider_model.content_probe_failure_count == 1, "超过 10 分钟的旧探针失败不得累计到本轮")
    _assert(provider_model.content_integrity_status == "degraded", "窗口外单次失败只应降级，不应直接 blocked")
    _assert(provider.content_integrity_status == "degraded", "provider 状态必须随模型窗口内失败汇总为 degraded")

    provider_model.content_probe_last_failed_at = datetime.utcnow()
    provider_model.content_probe_failure_count = 2
    ContentGuardProbeService.apply_content_probe_health(
        db,
        provider,
        provider_model,
        content_guard_result=review_result,
        endpoint_results=endpoint_results,
    )
    _assert(provider_model.content_probe_failure_count == 3, "10 分钟窗口内失败必须累计")
    _assert(provider_model.content_integrity_status == "blocked", "10 分钟窗口内 3 次失败必须隔离模型")
    _assert(provider.content_integrity_status == "blocked", "10 分钟窗口内 3 次失败必须隔离 provider")

    pass_result = {
        "content_guard_result": ContentGuardService.RESULT_PASS,
        "content_guard_reason": "可信探针通过",
        "content_guard_action": "allow",
    }
    pass_endpoint_results = [
        {
            "capability_key": key,
            "endpoint_label": key,
            "success": True,
            "content_guard": pass_result,
        }
        for key in ("content_fixed_answer", "content_pollution_rules", "content_sse")
    ]
    ContentGuardProbeService.apply_content_probe_health(
        db,
        provider,
        provider_model,
        content_guard_result=pass_result,
        endpoint_results=pass_endpoint_results,
    )
    _assert(provider_model.content_integrity_status == "passed", "blocked 模型通过可信探针后必须恢复为 passed")
    _assert(provider_model.circuit_state == "closed", "blocked 模型通过可信探针后必须关闭内容熔断")
    _assert(provider.content_integrity_status == "passed", "provider 在所有启用模型通过后必须自动恢复 passed")
    _assert(provider.circuit_state == "closed", "provider 在所有启用模型通过后必须关闭内容熔断")


def _check_trust_probe_keys_and_probe_boundaries() -> None:
    merged = ContentTrustProbeService.merge_required_probe_keys(["json", "tools", "fixed_answer"])
    _assert(merged == ["json", "tools", "fixed_answer", "pollution_rules", "sse"], f"可信探针必须保留用户勾选项并补齐必需项：{merged}")
    invalid = ContentTrustProbeService.invalid_probe(
        probe_key="frontend_unknown",
        endpoint_path="/responses",
        message="未知探针",
    )
    _assert(invalid["support_mode"] == "invalid_probe", f"未知探针必须显式失败而不是 skipped：{invalid}")
    _assert(invalid["success"] is False and invalid.get("content_guard"), f"未知探针失败必须带内容防护上下文：{invalid}")

    service_text = Path("app/services/content_guard_probe_service.py").read_text(encoding="utf-8")
    _assert("SSE_PROBE_MAX_CHUNKS" in service_text and "SSE_PROBE_MAX_BYTES" in service_text, "SSE 探针必须有 chunk 与字节边界")
    _assert("range(6)" not in service_text, "SSE 探针禁止固定只读取 6 个 chunk")
    _assert("POLLUTION_PROBE_SCENARIO_TIMEOUT_SECONDS" in service_text, "外链广告识别探针必须有单场景超时")
    _assert("POLLUTION_PROBE_TOTAL_TIMEOUT_SECONDS" in service_text, "外链广告识别探针必须有总超时")
    _assert("asyncio.wait_for" in service_text and "pollution_probe_timeout" in service_text, "外链广告识别探针必须实际执行超时控制")
    marker = ContentGuardProbeService.mark_detection_result(
        {
            "endpoint_path": "/responses",
            "endpoint_label": "固定答案完整性探针",
            "trace": [],
        }
    )
    _assert(marker["traffic_type"] == "content_guard_probe" and marker["is_detection_traffic"] is True, f"探针结果必须标记检测流量：{marker}")
    _assert(marker["trace"] and marker["trace"][0]["is_detection_traffic"] is True, f"探针 trace 必须标记检测流量：{marker}")


def _check_manual_trust_edit_trace() -> None:
    provider_model = ProviderModel(
        id=4402,
        provider_id=3301,
        model_name="stage33-manual-trust-model",
        enabled=True,
        content_integrity_status="passed",
    )
    ProviderService._ensure_manual_content_probe_reason(provider_model)
    payload = loads_json(provider_model.content_probe_results_json, {})
    _assert(provider_model.content_probe_last_passed_at is not None, "手动标记可信必须更新通过时间")
    _assert(provider_model.content_probe_failure_count == 0, "手动标记可信必须清空失败次数")
    _assert(payload.get("status") == "passed", f"手动可信明细必须记录 passed 状态：{payload}")
    _assert((payload.get("last_result") or {}).get("endpoint_label") == "管理员手动标记", f"手动可信明细必须可追溯：{payload}")
    phase_keys = {item.get("phase_key") for item in payload.get("results", []) if isinstance(item, dict)}
    _assert(
        {"content_fixed_answer", "content_pollution_rules"}.issubset(phase_keys),
        f"手动可信也必须按必需可信探针留下确认记录：{payload}",
    )
    _assert(ProviderService.provider_model_trust_status(provider_model) == "trusted", "手动标记可信后模型可信度应更新为可信")


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
    _assert(provider.content_integrity_score <= 20, f"自动隔离必须压低内容完整性分：{provider.content_integrity_score}")
    model = provider.provider_models[0]
    _assert(model.content_integrity_status == "blocked", f"自动隔离必须同步隔离挂载模型：{model.content_integrity_status}")
    _assert(model.circuit_state == "open", f"自动隔离必须同步熔断挂载模型：{model.circuit_state}")
    _assert(events["monitoring:content_guard_high_risk_provider:3302"]["payload"]["auto_isolated"] is True, "告警 payload 必须标记已自动隔离")

    changed_again = SystemMetricsService.apply_monitoring_alert_actions(fake_db, events)
    _assert(changed_again is False, "重复刷新已隔离提供商不应反复提交状态变更")


def _check_collect_applies_monitoring_actions_without_refresh() -> None:
    calls: list[dict] = []
    writes: list[dict] = []
    originals = {
        "_database_snapshot": SystemMetricsService._database_snapshot,
        "_limits_snapshot": SystemMetricsService._limits_snapshot,
        "_redis_snapshot": SystemMetricsService._redis_snapshot,
        "_runtime_snapshot": SystemMetricsService._runtime_snapshot,
        "_host_snapshot": SystemMetricsService._host_snapshot,
        "_safe_snapshot": SystemMetricsService._safe_snapshot,
        "_traffic_snapshot": SystemMetricsService._traffic_snapshot,
        "_bucket_minutes": SystemMetricsService._bucket_minutes,
        "_provider_snapshot": SystemMetricsService._provider_snapshot,
        "_content_guard_snapshot": SystemMetricsService._content_guard_snapshot,
        "_background_snapshot": SystemMetricsService._background_snapshot,
        "_database_pool_snapshot": SystemMetricsService._database_pool_snapshot,
        "_resolve_status": SystemMetricsService._resolve_status,
        "_evaluate_alerts": SystemMetricsService._evaluate_alerts,
        "apply_monitoring_actions": SystemMetricsService.apply_monitoring_actions,
        "write_monitoring_alerts": SystemMetricsService.write_monitoring_alerts,
        "metric_timeseries": LogService.metric_timeseries,
    }
    try:
        SystemMetricsService._database_snapshot = classmethod(lambda cls, db: {"ok": True, "status": "ok"})
        SystemMetricsService._limits_snapshot = classmethod(lambda cls, db: {})
        SystemMetricsService._redis_snapshot = classmethod(lambda cls: {"ok": True, "active_requests": 0, "active_streams": 0})
        SystemMetricsService._runtime_snapshot = classmethod(lambda cls: {})
        SystemMetricsService._host_snapshot = classmethod(lambda cls: {})
        SystemMetricsService._safe_snapshot = staticmethod(
            lambda db, section_errors, section_name, loader, fallback_factory: loader()
        )
        LogService.metric_timeseries = staticmethod(lambda db, *, window_minutes, bucket_minutes: [])
        SystemMetricsService._traffic_snapshot = classmethod(
            lambda cls, db, *, window_minutes: {"total_requests": 0, "status_5xx_rate": 0, "status_429": 0, "status_429_rate": 0.0}
        )
        SystemMetricsService._bucket_minutes = classmethod(lambda cls, window_minutes: 1)
        SystemMetricsService._provider_snapshot = classmethod(lambda cls, db, *, window_minutes: [])
        SystemMetricsService._content_guard_snapshot = classmethod(
            lambda cls, db, *, window_minutes: {
                **SystemMetricsService._empty_content_guard(),
                "high_risk_provider_counts": [
                    {
                        "provider_id": 3304,
                        "provider_name": "内容采集自动隔离测试提供商",
                        "high_risk_count": 3,
                        "window_minutes": 10,
                    }
                ],
            }
        )
        SystemMetricsService._background_snapshot = classmethod(
            lambda cls, db: {"pending_finalize_logs": 0, "billing_failed_logs": 0, "token_failed_logs": 0}
        )
        SystemMetricsService._database_pool_snapshot = classmethod(lambda cls: {})
        SystemMetricsService._resolve_status = classmethod(lambda cls, **kwargs: "ready")
        SystemMetricsService._evaluate_alerts = classmethod(lambda cls, metrics: [])
        SystemMetricsService.apply_monitoring_actions = classmethod(
            lambda cls, db, metrics: calls.append({"db": db, "metrics": metrics}) or True
        )
        SystemMetricsService.write_monitoring_alerts = classmethod(
            lambda cls, db, metrics: writes.append({"db": db, "metrics": metrics})
        )

        metrics = SystemMetricsService.collect(object(), window_minutes=5, refresh_alerts=False)
        _assert(calls, "collect(refresh_alerts=False) 也必须触发监控动作，避免自动隔离依赖手动刷新")
        _assert(not writes, "refresh_alerts=False 时不应写入告警事件")
        _assert(metrics["content_guard"]["high_risk_provider_counts"][0]["provider_id"] == 3304, "采集指标应保留内容高风险明细")
    finally:
        for name, value in originals.items():
            if name == "metric_timeseries":
                setattr(LogService, name, value)
            else:
                setattr(SystemMetricsService, name, value)


async def _check_distributed_lock_skips_when_redis_unavailable() -> None:
    from app import tasks as tasks_module

    calls: list[str] = []
    records: list[dict] = []

    class _UnavailableRedisService:
        @staticmethod
        def get_client():
            raise RuntimeError("redis unavailable for regression")

    original_redis_service = tasks_module.RedisService
    original_record = tasks_module._safe_record_job_event
    try:
        tasks_module.RedisService = _UnavailableRedisService
        tasks_module._safe_record_job_event = lambda **kwargs: records.append(kwargs) or 3304

        @tasks_module.distributed_job_lock("stage33_lock_unavailable", ttl_seconds=1)
        async def _job():
            calls.append("ran")
            return {"processed_count": 1}

        result = await _job()
        _assert(result is None, "Redis 锁不可用时后台任务应跳过并返回 None")
        _assert(not calls, "Redis 锁不可用时禁止无锁执行任务函数体")
        _assert(records, "Redis 锁不可用跳过必须记录后台任务日志")
        _assert(records[0]["lock_status"] == "unavailable_skipped", f"锁状态应标记不可用跳过：{records[0]}")
        _assert(records[0]["status"] == "skipped_lock_unavailable", f"任务状态应标记锁不可用跳过：{records[0]}")
    finally:
        tasks_module.RedisService = original_redis_service
        tasks_module._safe_record_job_event = original_record


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


def _expect_validation_error(factory, message: str) -> None:
    try:
        factory()
    except Exception:
        return
    raise AssertionError(message)


def _check_external_probe_security_boundaries() -> None:
    def target(**kwargs):
        payload = {
            "base_url": "https://example.com/v1",
            "api_key": "sk-stage33",
            "model_name": "stage33-model",
        }
        payload.update(kwargs)
        return ContentGuardExternalTarget(**payload)

    _assert(target().base_url == "https://example.com/v1", "外部检测应接受公网 HTTPS 地址")
    for bad_url in (
        "http://example.com/v1",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.2/v1",
        "https://172.16.0.2/v1",
        "https://192.168.1.10/v1",
        "https://169.254.169.254/latest",
    ):
        _expect_validation_error(
            lambda bad_url=bad_url: target(base_url=bad_url),
            f"外部检测必须拒绝不安全地址：{bad_url}",
        )
    _expect_validation_error(lambda: target(api_key="k" * 4097), "外部检测 API Key 必须限制最大长度")
    _expect_validation_error(lambda: target(model_name="m" * 257), "外部检测模型名必须限制最大长度")


class _FakeSettingsDb:
    def __init__(self) -> None:
        self.commit_count = 0
        self.refreshed = None

    def commit(self) -> None:
        self.commit_count += 1

    def refresh(self, obj) -> None:
        self.refreshed = obj


class _ProbeHealthSession:
    def __init__(self) -> None:
        self.commit_count = 0

    def commit(self) -> None:
        self.commit_count += 1

    def refresh(self, obj) -> None:
        self.refreshed = obj


def _check_settings_submit_affects_runtime() -> None:
    setting = type(
        "Setting",
        (),
        {
            "content_guard_enabled": True,
            "content_guard_precheck_auto_enabled": True,
            "content_guard_block_on_high_risk": True,
            "content_guard_probe_interval_sec": 3600,
            "content_guard_max_scan_bytes": 16384,
            "content_guard_stream_buffer_max_bytes": 16384,
            "content_guard_low_trust_requires_buffer": True,
            "content_guard_high_risk_strategy": "switch_provider",
            "content_guard_max_detection_delay_ms": 300,
            "content_guard_stream_mode": "buffer_300ms",
            "content_guard_url_check_enabled": True,
            "content_guard_url_allowlist_json": "",
            "content_guard_async_review_enabled": True,
            "content_guard_high_risk_confidence_threshold": 85,
        },
    )()
    fake_db = _FakeSettingsDb()
    original_get_or_create = content_guard_module_service.SettingService.get_or_create
    content_guard_module_service.SettingService.get_or_create = staticmethod(lambda db: setting)
    try:
        updated = ContentGuardModuleService.update_settings(
            fake_db,
            ContentGuardSettingsUpdate(
                content_guard_enabled=False,
                content_guard_block_on_high_risk=False,
                content_guard_high_risk_strategy="record_only",
                content_guard_max_detection_delay_ms=123,
            ),
        )
    finally:
        content_guard_module_service.SettingService.get_or_create = original_get_or_create
    _assert(fake_db.commit_count == 1 and fake_db.refreshed is setting, "内容防护设置提交必须落库并刷新")
    _assert(updated.content_guard_enabled is False, "设置提交必须更新 content_guard_enabled")
    _assert(updated.content_guard_high_risk_strategy == "record_only", "设置提交必须更新高风险策略")
    _assert(updated.content_guard_max_detection_delay_ms == 123, "设置提交必须更新流式检测延迟")
    _assert(
        ContentRuntimeGuardService.enabled_for_request(
            setting=updated,
            provider=_provider(content_guard_enabled=True),
            route_context=_context(),
        )
        is False,
        "关闭内容防护设置后运行时检测必须立即停用",
    )


def _check_content_guard_migrations_build_typed_tables() -> None:
    migration = Path("migrations/2026-06-06_add_content_guard_governance.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE request_logs (id INTEGER PRIMARY KEY)")
        for table_name in ("request_content_guard_events", "health_probe_events"):
            match = re.search(
                rf"CREATE TABLE IF NOT EXISTS {table_name}\s*\((.*?)\);",
                migration,
                re.DOTALL,
            )
            _assert(match is not None, f"迁移必须显式创建 typed logging 表：{table_name}")
            conn.executescript(f"CREATE TABLE IF NOT EXISTS {table_name} ({match.group(1)});")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            _assert(exists is not None, f"空库执行迁移片段后应存在表：{table_name}")
    finally:
        conn.close()
    strategy_migration = Path("migrations/2026-06-07_extend_content_guard_strategy_metrics.sql").read_text(
        encoding="utf-8"
    )
    for needle in (
        "ix_request_logs_content_guard_risk_created_provider",
        "ix_request_logs_content_guard_final_strategy",
        "ix_request_logs_content_guard_retry_provider_count",
        "ix_request_logs_content_guard_buffer_wait",
    ):
        _assert(
            needle in migration or needle in strategy_migration,
            f"内容防护日志治理迁移缺少索引：{needle}",
        )


def _check_content_guard_browser_smoke() -> None:
    smoke_url = os.environ.get("STAGE33_BROWSER_SMOKE_URL")
    if not smoke_url:
        return
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise AssertionError("启用 STAGE33_BROWSER_SMOKE_URL 时必须安装 Playwright 并完成浏览器安装") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(smoke_url, wait_until="domcontentloaded")
            page.locator("#content-guard-refresh-btn").click()
            page.locator("input[name='content_guard_target_type'][value='external']").check(force=True)
            page.locator("#content-guard-external-base-url").fill("https://example.com/v1")
            page.locator("#content-guard-external-api-key").fill("sk-stage33")
            page.locator("#content-guard-external-model-name").fill("stage33-model")
            page.locator("#content-guard-inspect-text").fill("这是一段本地检测文本")
            _assert(
                page.locator("#content-guard-probe-form").count() == 1
                and page.locator("#content-guard-inspect-form").count() == 1,
                "内容防护页面必须渲染可信探测和本地检测表单",
            )
        finally:
            browser.close()


def _check_frontend_and_log_wiring() -> None:
    checks = [
        ("app/templates/providers.html", [
            "provider-trust-level",
            "provider-content-integrity-status",
        ]),
        ("app/templates/api_keys.html", []),
        ("app/templates/user_api_keys.html", [
            "管理员统一要求",
        ]),
        ("app/routers/user_portal.py", []),
        ("app/services/user_portal_service.py", [
            "require_trusted_provider=bool(getattr(route_setting, \"trusted_providers_only\", False))",
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
            "content_guard_action: str | None = None",
            "content_guard_final_strategy: str | None = None",
            "content_guard_retry_count: int | None = Query(default=None, ge=0)",
            "content_guard_guard_stage: str | None = None",
        ]),
        ("app/services/log_service.py", [
            "content_guard_action: str | None = None",
            "content_guard_final_strategy: str | None = None",
            "content_guard_retry_count: int | None = None",
            "content_guard_guard_stage: str | None = None",
            "content_guard_retry_provider_count",
        ]),
        ("app/schemas/content_guard.py", [
            "外部渠道接口地址必须使用 https://",
            "ipaddress.ip_address",
            "max_length=4096",
            "max_length=256",
        ]),
        ("app/services/proxy_service.py", [
            "response_payload=client_response",
            "guard_stage = \"stream_buffer\" if stream_guard_buffering else",
            "后续仍会按分块滑动窗口执行内容完整性扫描",
        ]),
        ("migrations/2026-06-06_add_content_guard_governance.sql", [
            "CREATE TABLE IF NOT EXISTS request_content_guard_events",
            "CREATE TABLE IF NOT EXISTS health_probe_events",
            "ix_request_logs_content_guard_risk_created_provider",
        ]),
        ("migrations/2026-06-07_extend_content_guard_strategy_metrics.sql", [
            "ix_request_logs_content_guard_final_strategy",
            "ix_request_logs_content_guard_retry_provider_count",
            "ix_request_logs_content_guard_buffer_wait",
        ]),
        ("app/templates/base.html", [
            "?v=20260608-",
            "/content-guard",
            "内容防护",
        ]),
        ("app/templates/content_guard.html", [
            "content-guard-settings-form",
            "content-guard-probe-form",
            "content-guard-external-base-url",
            "content-guard-external-api-key-toggle",
            "content-guard-external-api-key-clear",
            "不含 /responses 或 /chat/completions",
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
            '"/trust-probe"',
        ]),
        ("app/services/content_guard_module_service.py", [
            "ContentGuardModuleService",
            "build_overview",
            "update_settings",
            "update_rules",
            "inspect_text",
            "run_probe",
            "run_trust_probe",
            "ContentTrustProbeService",
            "run_capability_probe",
        ]),
        ("app/services/content_trust_probe_service.py", [
            "class ContentTrustProbeService",
            "REQUIRED_TRUST_PROBE_KEYS = [\"fixed_answer\", \"pollution_rules\", \"sse\"]",
            "run_trust_probe",
            "update_provider_model_trust_status",
            "get_trust_decision_for_route",
            "_external_target_identity",
            "target_id",
        ]),
        ("app/services/content_runtime_guard_service.py", [
            "class ContentRuntimeGuardService",
            "inspect_non_stream_response",
            "inspect_stream_prefetch_buffer",
            "inspect_stream_chunk",
            "decide_runtime_action",
        ]),
        ("app/static/js/app.js", [
            "/api/content-guard/rules",
            "/api/content-guard/rules/reset",
            "/api/content-guard/runtime/inspect-text",
            "/api/content-guard/precheck/probe",
            "/api/content-guard/trust-probe",
            "renderContentGuardProbeModalBody",
            "data-action=\"trust-binding\"",
            "content-guard-rules-body",
            "content-guard-inspect-form",
            "formatContentGuardActionLabel",
            "检测明细",
            "通过 ${passed}/${total}",
            "normalizeRuleId",
            "nextCustomRuleId",
            "readNumberField",
            "new RegExp(normalizedPattern)",
            "重复匹配项",
            "放行动作时扣分必须为 0",
            "拦截动作时风险等级必须为高",
            "externalApiKeyInput.value = \"\"",
            "content-guard-external-api-key-toggle",
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
    trust_probe_text = Path("app/services/content_trust_probe_service.py").read_text(encoding="utf-8", errors="ignore")
    _assert("Provider(\n                id=0" not in trust_probe_text, "外部检测禁止继续构造 Provider(id=0)")
    _assert("ProviderModel(\n                id=0" not in trust_probe_text, "外部检测禁止继续构造 ProviderModel(id=0)")
    _assert('"base_url": external.base_url' not in trust_probe_text, "外部检测返回给前端的 target 禁止包含完整 base_url")
    app_css = Path("app/static/css/app.css").read_text(encoding="utf-8", errors="ignore")
    _assert(".content-guard-segment input:focus-visible + span" in app_css, "内容防护分段切换必须具备键盘焦点样式")
    _assert(".content-guard-probe-option input:checked + span::before" in app_css, "内容防护探针多选禁止裸露浏览器默认 checkbox")
    _assert('body[data-page="content-guard"] .content-guard-rules-table' in app_css and "table-layout: auto;" in app_css, "内容防护规则表移动端必须取消硬宽并使用卡片化披露")
    content_guard_template = Path("app/templates/content_guard.html").read_text(encoding="utf-8", errors="ignore")
    _assert("用户请求中出现过的域名会自动加入本次白名单" not in content_guard_template, "URL 白名单说明禁止暗示请求体可自动放行域名")


def main() -> None:
    _check_content_guard_detection()
    _check_response_structure_detection()
    _check_router_content_policy()
    _check_runtime_guard_request_semantics()
    _check_low_trust_route_field_hidden()
    _check_high_risk_route_detection()
    _check_content_guard_route_cache_and_scheduler_policy()
    _check_content_guard_frontend_policy_wiring()
    _check_error_catalog()
    _check_health_probe_guard_helpers()
    _check_content_probe_health_window_and_recovery()
    _check_trust_probe_keys_and_probe_boundaries()
    _check_manual_trust_edit_trace()
    _check_content_guard_metrics_alerts()
    _check_content_guard_auto_isolation()
    _check_collect_applies_monitoring_actions_without_refresh()
    asyncio.run(_check_distributed_lock_skips_when_redis_unavailable())
    _check_content_guard_disabled_mode()
    _check_external_probe_security_boundaries()
    _check_settings_submit_affects_runtime()
    _check_content_guard_migrations_build_typed_tables()
    _check_content_guard_browser_smoke()
    _check_frontend_and_log_wiring()
    print("stage33 content guard regression check passed")


if __name__ == "__main__":
    main()
