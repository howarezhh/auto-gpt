from __future__ import annotations

import asyncio
import httpx
import os
import re
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.utils.json_utils import dumps_json, loads_json

from app.models.alert_event import AlertEvent
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.schemas.content_guard import ContentGuardExternalTarget, ContentGuardRulesUpdate, ContentGuardRunRequest, ContentGuardSettingsUpdate, ContentGuardTextInspectRequest
from app.schemas.setting import SettingUpdate
from app.utils.content_guard_config import CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT, CONTENT_GUARD_STREAM_BUFFER_MAX_BYTES_LIMIT
from app.services.content_guard_probe_service import ContentGuardProbeService
import app.services.content_guard_module_service as content_guard_module_service
from app.services.content_guard_module_service import ContentGuardModuleService
from app.services.content_guard_rule_service import ContentGuardRuleService
from app.services.content_guard_service import ContentGuardResult, ContentGuardService
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
import app.services.health_service as health_service_module
import app.routers.logging_api as logging_api
import app.routers.content_guard as content_guard_router
from app.database import get_db


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class _ContentGuardHtmlContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.result_headers: list[str] = []
        self._inside_result_table = False
        self._inside_th = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value for key, value in attrs}
        node_id = attrs_dict.get("id")
        if node_id:
            self.ids.add(node_id)
        if tag == "div" and attrs_dict.get("class") == "table-shell content-guard-result-table":
            self._inside_result_table = True
        if tag == "th" and self._inside_result_table:
            self._inside_th = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._inside_result_table:
            self._inside_result_table = False
        if tag == "th":
            self._inside_th = False

    def handle_data(self, data: str) -> None:
        if self._inside_th:
            text = data.strip()
            if text:
                self.result_headers.append(text)


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
    strict_link = ContentGuardService.inspect_response_text(
        "这里有一个参考链接：https://openai.com/docs",
        request_payload={"messages": [{"role": "user", "content": "写一段普通说明"}]},
        url_allowlist="[]",
    )
    _assert(strict_link.result == ContentGuardService.RESULT_REVIEW, f"空白名单必须支持严格无默认外链模式：{strict_link}")
    allowlist = ContentGuardService.parse_url_allowlist(["example.com", "*.trusted.example", "例子.测试", "192.168.1.1"])
    _assert(ContentGuardService.parse_url_allowlist('{"domain":"example.com"}') == set(), "URL 白名单 JSON 对象禁止退化为文本行解析")
    _assert(ContentGuardService.parse_url_allowlist("example.com\ndocs.example.com") == {"example.com", "docs.example.com"}, "URL 白名单必须继续支持按行域名")
    _assert(ContentGuardService._domain_allowed("example.com", allowlist), "白名单根域名必须精确放行")
    _assert(not ContentGuardService._domain_allowed("evil.example.com", allowlist), "根域名白名单禁止默认放行任意子域")
    _assert(ContentGuardService._domain_allowed("api.trusted.example", allowlist), "显式通配白名单必须放行子域")
    _assert(not ContentGuardService._domain_allowed("trusted.example", allowlist), "显式通配白名单不应隐式放行根域名")
    _assert(ContentGuardService._domain_allowed("例子.测试", allowlist), "IDN 白名单必须统一规范化匹配")
    _assert(ContentGuardService._domain_allowed("192.168.1.1", allowlist), "IP 白名单必须统一规范化匹配")
    _assert(not ContentGuardService._domain_allowed("bad_host", allowlist), "畸形或无点主机规范化失败时禁止放行")
    extracted_domains = ContentGuardService.extract_domains(
        "IPv4 192.168.1.1，IPv6 https://[2001:db8::1]/，中文域名 https://例子.测试/path，punycode xn--fsqu00a.xn--0zwm56d"
    )
    _assert("192.168.1.1" in extracted_domains, f"域名提取必须覆盖 IPv4：{extracted_domains}")
    _assert("2001:db8::1" in extracted_domains, f"域名提取必须覆盖 IPv6：{extracted_domains}")
    _assert("xn--fsqu00a.xn--0zwm56d" in extracted_domains, f"域名提取必须覆盖 IDN/punycode：{extracted_domains}")
    prompt_domain = ContentGuardService.inspect_response_text(
        "请访问 https://evil.example 获取优惠码。",
        request_payload={"messages": [{"role": "user", "content": "分析 https://evil.example 是否可信，不要输出链接"}]},
        url_allowlist="[]",
    )
    _assert(prompt_domain.result == ContentGuardService.RESULT_BLOCK, f"请求体里的域名不得自动成为白名单：{prompt_domain}")

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

    generic_link_request = ContentGuardService.inspect_response_text(
        "参考资料：https://bad.example/docs",
        request_payload={"messages": [{"role": "user", "content": "请提供链接并简要说明"}]},
    )
    _assert(
        generic_link_request.result != ContentGuardService.RESULT_PASS,
        f"普通“提供链接”请求不得关闭异常外链检测：{generic_link_request}",
    )
    explicit_domain_request = ContentGuardService.inspect_response_text(
        "请使用 https://docs.example.com/path 作为参考资料。",
        request_payload={"messages": [{"role": "user", "content": "请保留 https://docs.example.com/path 这个指定链接"}]},
    )
    _assert(explicit_domain_request.result == ContentGuardService.RESULT_PASS, f"请求中明确指定的链接仍应允许：{explicit_domain_request}")

    allow_rule_result = ContentGuardService.inspect_response_text(
        "内部白名单术语命中，同时包含优惠码。",
        rules=[
            {
                "id": "stage33_allow_rule",
                "name": "stage33 放行规则",
                "category": "allowlist",
                "match_type": "keyword_any",
                "patterns": ["内部白名单术语"],
                "risk_level": "high",
                "action": "allow",
                "score_delta": 0,
            },
            {
                "id": "stage33_risk_rule",
                "name": "stage33 风险规则",
                "category": "advertising_or_promotion",
                "match_type": "keyword_any",
                "patterns": ["优惠码"],
                "risk_level": "high",
                "action": "block",
                "score_delta": -25,
            },
        ],
    )
    _assert(allow_rule_result.result == ContentGuardService.RESULT_PASS, f"allow 规则必须具备真正放行语义：{allow_rule_result}")
    _assert(allow_rule_result.action == "allow" and allow_rule_result.score_delta == 0, f"allow 规则禁止继续参与扣分或阻断：{allow_rule_result}")

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


def _check_content_guard_rule_service_boundaries() -> None:
    service_text = Path("app/services/content_guard_service.py").read_text(encoding="utf-8")
    _assert("DEFAULT_URL_ALLOWLIST" not in service_text, "内容防护 URL 白名单不得继续保留不可审计的服务端默认放行域名")
    match_body = service_text.split("def match_text_rules", 1)[1].split("def inspect_json_response", 1)[0]
    _assert(
        "ContentGuardRuleService.match_text_rules" in match_body,
        "运行时内容检测必须委托 ContentGuardRuleService 执行规则匹配",
    )

    invalid_json = ContentGuardService.inspect_response_text("普通文本", rules_json="{")
    _assert(invalid_json.result == ContentGuardService.RESULT_ERROR, f"规则 JSON 损坏必须显式报错：{invalid_json}")
    _assert("rule_configuration_error" in invalid_json.categories, f"规则 JSON 损坏必须记录配置错误分类：{invalid_json}")

    duplicate_rules = [
        {"id": "same_rule", "name": "规则一", "category": "custom", "patterns": ["不会命中"]},
        {"id": "same_rule", "name": "规则二", "category": "custom", "patterns": ["普通文本"]},
    ]
    duplicate_result = ContentGuardService.inspect_response_text("普通文本", rules=duplicate_rules)
    _assert(duplicate_result.result == ContentGuardService.RESULT_ERROR, f"重复规则 ID 必须显式报错：{duplicate_result}")

    invalid_enum = ContentGuardService.inspect_response_text(
        "普通文本",
        rules=[{"id": "bad_enum", "name": "坏枚举", "category": "custom", "match_type": "wildcard", "patterns": ["普通文本"]}],
    )
    _assert(invalid_enum.result == ContentGuardService.RESULT_ERROR, f"非法规则枚举禁止静默降级：{invalid_enum}")

    invalid_regex = ContentGuardService.inspect_response_text(
        "普通文本",
        rules=[{"id": "bad_regex", "name": "坏正则", "category": "custom", "match_type": "regex", "patterns": ["["]}],
    )
    _assert(invalid_regex.result == ContentGuardService.RESULT_ERROR, f"非法正则必须显式报错：{invalid_regex}")

    unsafe_regex = ContentGuardService.inspect_response_text(
        "aaaaaaaaaaaaaaaa",
        rules=[{"id": "unsafe_regex", "name": "高风险正则", "category": "custom", "match_type": "regex", "patterns": ["(a+)+$"]}],
    )
    _assert(unsafe_regex.result == ContentGuardService.RESULT_ERROR, f"高复杂度正则必须被规则层拒绝：{unsafe_regex}")

    allow_result = ContentGuardService.inspect_response_text(
        "专用豁免词",
        rules=[
            {
                "id": "allow_phrase",
                "name": "专用放行",
                "category": "custom",
                "action": "allow",
                "score_delta": 0,
                "patterns": ["专用豁免词"],
            },
            {
                "id": "block_phrase",
                "name": "专用拦截",
                "category": "custom",
                "risk_level": "high",
                "action": "block",
                "score_delta": -30,
                "patterns": ["专用豁免词"],
            },
        ],
    )
    _assert(allow_result.result == ContentGuardService.RESULT_PASS and allow_result.action == "allow", f"allow 规则命中必须短路放行：{allow_result}")

    positive_score = ContentGuardService.inspect_response_text(
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
    _assert(positive_score.score_delta == 20, f"规则评分必须保留非负分值语义，禁止 abs 反向加重风险：{positive_score}")


def _check_response_structure_detection() -> None:
    chat_result = ContentGuardService.inspect_json_response(
        {"id": "chatcmpl-stage33"},
        endpoint_path="/chat/completions",
    )
    _assert(chat_result.result == ContentGuardService.RESULT_BLOCK, f"Chat 响应缺 choices 应拦截：{chat_result}")
    _assert("chat_completion_schema_violation" in chat_result.categories, f"Chat 结构分类缺失：{chat_result}")

    empty_chat_result = ContentGuardService.inspect_json_response(
        {"id": "chatcmpl-stage33", "choices": []},
        endpoint_path="/chat/completions",
    )
    _assert(empty_chat_result.result == ContentGuardService.RESULT_BLOCK, f"Chat 空 choices 应拦截：{empty_chat_result}")
    _assert("chat_completion_schema_violation" in empty_chat_result.categories, f"Chat 空 choices 分类缺失：{empty_chat_result}")

    responses_result = ContentGuardService.inspect_json_response(
        {"id": "resp-stage33"},
        endpoint_path="/responses",
    )
    _assert(responses_result.result == ContentGuardService.RESULT_BLOCK, f"Responses 响应缺 output 应拦截：{responses_result}")
    _assert("responses_schema_violation" in responses_result.categories, f"Responses 结构分类缺失：{responses_result}")

    empty_responses_result = ContentGuardService.inspect_json_response(
        {"id": "resp-stage33", "output": [], "output_text": ""},
        endpoint_path="/responses",
    )
    _assert(empty_responses_result.result == ContentGuardService.RESULT_BLOCK, f"Responses 空 output 应拦截：{empty_responses_result}")
    _assert("responses_schema_violation" in empty_responses_result.categories, f"Responses 空 output 分类缺失：{empty_responses_result}")

    http_200_error = ContentGuardService.inspect_json_response(
        {"error": {"message": "上游返回错误体", "code": "bad_upstream"}},
        endpoint_path="/chat/completions",
    )
    _assert(http_200_error.result == ContentGuardService.RESULT_BLOCK, f"HTTP 200 error 体应拦截：{http_200_error}")
    _assert("upstream_error_payload" in http_200_error.categories, f"HTTP 200 error 体分类缺失：{http_200_error}")

    sse_result = ContentGuardService.inspect_sse_event("not-json", endpoint_path="/chat/completions")
    _assert(sse_result.result == ContentGuardService.RESULT_REVIEW, f"SSE 非 JSON 兼容事件应降级复核而非直接阻断：{sse_result}")
    _assert(sse_result.risk_level == "medium", f"SSE 非 JSON 兼容事件应标记中风险复核：{sse_result}")
    _assert("invalid_sse_event" in sse_result.categories, f"SSE 分类缺失：{sse_result}")

    suspicious_field = ContentGuardService.inspect_json_response(
        {"id": "chatcmpl-stage33", "choices": [{"message": {"content": "普通解释"}}], "promo": "扫码联系代理"},
        endpoint_path="/chat/completions",
        request_payload={"messages": [{"role": "user", "content": "解释装饰器"}]},
    )
    _assert(suspicious_field.result == ContentGuardService.RESULT_BLOCK, f"广告字段应拦截：{suspicious_field}")
    _assert("unexpected_advertising_field" in suspicious_field.categories, f"广告字段分类缺失：{suspicious_field}")

    business_json_key = ContentGuardService.inspect_json_response(
        {"choices": [{"message": {"content": {"contact": "Alice", "promo": "internal campaign note"}}}]},
        endpoint_path="/chat/completions",
        request_payload={"messages": [{"role": "user", "content": "按 JSON 返回联系人字段"}]},
    )
    _assert(business_json_key.result == ContentGuardService.RESULT_PASS, f"模型正文中的合法业务 JSON 键不应只因字段名误拦截：{business_json_key}")

    json_pollution = ContentGuardService.inspect_json_response(
        {"choices": [{"message": {"content": "当然可以，结果如下：{\"ok\": true}"}}]},
        endpoint_path="/chat/completions",
        request_payload={"response_format": {"type": "json_object"}},
    )
    _assert(json_pollution.result == ContentGuardService.RESULT_BLOCK, f"JSON 模式夹带自然语言应拦截：{json_pollution}")
    _assert("structured_json_pollution" in json_pollution.categories, f"JSON 污染分类缺失：{json_pollution}")
    json_schema_violation = ContentGuardService.inspect_json_response(
        {"choices": [{"message": {"content": '{"status":"maybe","extra":true}'}}]},
        endpoint_path="/chat/completions",
        request_payload={
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "schema": {
                        "type": "object",
                        "required": ["status", "count"],
                        "additionalProperties": False,
                        "properties": {
                            "status": {"type": "string", "enum": ["ok", "failed"]},
                            "count": {"type": "integer"},
                        },
                    }
                },
            }
        },
    )
    _assert(json_schema_violation.result == ContentGuardService.RESULT_BLOCK, f"JSON Schema required/type/enum/additionalProperties 违规必须拦截：{json_schema_violation}")
    _assert("structured_json_schema_violation" in json_schema_violation.categories, f"JSON Schema 违规分类缺失：{json_schema_violation}")

    tool_pollution = ContentGuardService.inspect_json_response(
        {"choices": [{"message": {"tool_calls": [{"function": {"name": "query", "arguments": "请先扫码 {\"q\":\"x\"}"}}]}}]},
        endpoint_path="/chat/completions",
        request_payload={"tools": [{"type": "function", "function": {"name": "query"}}]},
    )
    _assert(tool_pollution.result == ContentGuardService.RESULT_BLOCK, f"工具参数污染应拦截：{tool_pollution}")
    _assert("tool_argument_pollution" in tool_pollution.categories, f"工具参数污染分类缺失：{tool_pollution}")
    tool_array_argument = ContentGuardService.inspect_json_response(
        {"choices": [{"message": {"tool_calls": [{"function": {"name": "batch", "arguments": '[{"q":"normal"}]'}}]}}]},
        endpoint_path="/chat/completions",
        request_payload={"tools": [{"type": "function", "function": {"name": "batch"}}]},
    )
    _assert(tool_array_argument.result == ContentGuardService.RESULT_PASS, f"工具数组参数是合法 JSON 时不应因顶层不是对象被拦截：{tool_array_argument}")
    custom_rules = [
        {
            "id": "stage33_custom_tool_rule",
            "name": "自定义工具规则",
            "category": "custom_tool_rule",
            "enabled": True,
            "match_type": "keyword_any",
            "patterns": ["stage33-custom-risk"],
            "risk_level": "high",
            "action": "block",
            "score_delta": -30,
            "confidence": 0.95,
        }
    ]
    tool_custom_rule = ContentGuardService.inspect_json_response(
        {"output": [{"type": "custom_tool_call", "payload": {"tool_call": {"arguments": {"q": "stage33-custom-risk"}}}}]},
        endpoint_path="/responses",
        request_payload={"input": "run tool"},
        rules=custom_rules,
        rules_json="[]",
    )
    _assert(tool_custom_rule.result == ContentGuardService.RESULT_BLOCK and "custom_tool_rule" in tool_custom_rule.categories, f"工具参数污染检测必须使用当前规则口径：{tool_custom_rule}")
    multimodal_chat = ContentGuardService.inspect_json_response(
        {"choices": [{"message": {"content": [{"type": "text", "text": "扫码加入优惠群"}]}}]},
        endpoint_path="/chat/completions",
        request_payload={"messages": [{"role": "user", "content": "解释装饰器"}]},
    )
    _assert(multimodal_chat.result == ContentGuardService.RESULT_BLOCK, f"Chat 多模态 content 文本必须被扫描：{multimodal_chat}")
    responses_summary = ContentGuardService.inspect_json_response(
        {"output": [{"type": "reasoning", "summary": [{"text": "访问 https://bad.example 领取优惠码"}]}]},
        endpoint_path="/responses",
        request_payload={"input": "解释装饰器"},
    )
    _assert(responses_summary.result == ContentGuardService.RESULT_BLOCK, f"Responses summary/reasoning 嵌套文本必须被扫描：{responses_summary}")
    responses_new_shapes = ContentGuardService.inspect_json_response(
        {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "正常主体"}]},
                {"type": "message", "delta": {"text": "扫码加入优惠群"}},
            ],
            "metadata": {"audit_note": "https://bad.example"},
        },
        endpoint_path="/responses",
        request_payload={"input": "解释装饰器"},
    )
    _assert(responses_new_shapes.result == ContentGuardService.RESULT_BLOCK, f"Responses output/delta/metadata 新结构文本必须参与扫描：{responses_new_shapes}")
    image_url_text = ContentGuardService._extract_scan_text(
        {"image_url": {"url": "https://image.example/a.png", "detail": "扫码领取优惠"}},
        max_scan_bytes=4096,
    )
    _assert("image.example" in image_url_text and "扫码领取优惠" in image_url_text, "image_url 对象中的远程 URL 和文本元数据必须进入扫描文本")
    deep_payload: object = "底部文本"
    for _ in range(80):
        deep_payload = {"next": [deep_payload]}
    extracted_deep = ContentGuardService._extract_scan_text(deep_payload, max_scan_bytes=4096)
    _assert(isinstance(extracted_deep, str), "深层 JSON 扫描必须受深度/节点上限保护并安全返回")


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
        self.added: list[object] = []

    def get(self, model, item_id):
        if model is Provider and item_id == self.provider.id:
            return self.provider
        return None

    def scalar(self, statement):
        return None

    def add(self, item) -> None:
        self.added.append(item)

    def commit(self) -> None:
        self.commit_count += 1


class _FakeProbeTargetSession:
    def __init__(self, provider: Provider, provider_model: ProviderModel) -> None:
        self.provider = provider
        self.provider_model = provider_model

    def get(self, model, item_id):
        if model is Provider and item_id == self.provider.id:
            return self.provider
        if model is ProviderModel and item_id == self.provider_model.id:
            return self.provider_model
        return None


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
    _assert(
        RouterService._content_policy_diagnostic_reason(low_score, route_context=_context(require_trusted_provider=False))
        == "provider_content_integrity_score_too_low",
        "API Key 内容检测可选不得放行内容完整性低分 provider",
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
    blocked_model_provider = _provider(trust_level="trusted", content_integrity_status="passed", content_integrity_score=90)
    blocked_model = ProviderModel(
        id=4404,
        provider_id=blocked_model_provider.id,
        model_name="stage33-blocked-model",
        enabled=True,
        content_integrity_status="blocked",
    )
    model_decision = ContentTrustProbeService.get_trust_decision_for_route(
        blocked_model_provider,
        provider_model=blocked_model,
        route_context=_context(),
    )
    _assert(model_decision["reason"] == "model_content_integrity_blocked", f"模型 blocked 必须独立硬排除：{model_decision}")
    _assert(model_decision["model_content_integrity_score"] == 0, f"模型 blocked 必须有模型级评分语义：{model_decision}")
    _assert(
        RouterService._provider_model_blocked_by_content_policy(blocked_model, provider=blocked_model_provider, route_context=_context()) is True,
        "模型内容完整性 blocked 不得被 provider 高分掩盖",
    )
    route_service_text = Path("app/services/router_service.py").read_text(encoding="utf-8")
    route_score_body = route_service_text.split("def _route_score", 1)[1].split("def _provider_blocked_by_content_policy", 1)[0]
    _assert("trust_level" not in route_score_body, "trust_level=blocked 必须由内容策略硬过滤，禁止混入路由打分惩罚")
    _assert("content_integrity" not in route_score_body, "内容完整性必须由内容策略硬过滤，禁止混入路由打分职责")


def _check_runtime_guard_request_semantics() -> None:
    setting_enabled = type(
        "Setting",
        (),
        {
            "content_guard_enabled": True,
            "content_guard_stream_mode": "buffer_300ms",
            "content_guard_async_review_enabled": True,
            "content_guard_high_risk_strategy": "switch_provider",
        },
    )()
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
            setting=setting_enabled,
            provider=provider_enabled,
            route_context=_context(),
        ) is True,
        "API Key 内容检测可选不等于允许关闭全局运行时内容防护",
    )
    _assert(
        ContentRuntimeGuardService.enabled_for_request(
            setting=setting_enabled,
            provider=provider_enabled,
            route_context=_context(content_guard_required=False),
        ) is False,
        "content_guard_required=False 必须真实关闭该请求的运行时内容防护语义",
    )
    _assert(
        ContentRuntimeGuardService.enabled_for_request(
            setting=setting_disabled,
            provider=provider_enabled,
            route_context=route_context,
        ) is False,
        "内容完整性全局总开关关闭时必须停用请求过程检测",
    )
    review_result = ContentGuardResult(
        result=ContentGuardRuleService.RESULT_REVIEW,
        risk_level="medium",
        categories=["unexpected_link"],
        reason="中风险复核",
        action="record",
    )
    _assert(
        ContentRuntimeGuardService.should_record_violation(review_result, setting=setting_enabled) is True,
        "异步复核开启时 review 结果必须记录，不能既不阻断也不入违规状态",
    )
    _assert(
        ContentRuntimeGuardService.should_block_for_response(review_result, setting=setting_enabled) is False,
        "review + async_review 应记录但不应按高风险阻断",
    )
    _assert(
        ContentRuntimeGuardService.stream_should_buffer(setting=setting_enabled, provider=provider_enabled, route_context=_context()) is True,
        "标准信任 provider 在全局内容防护开启时也必须执行首段缓冲保护",
    )
    _assert(
        ContentRuntimeGuardService.stream_should_buffer(
            setting=setting_enabled,
            provider=provider_enabled,
            route_context=_context(require_trusted_provider=True),
        )
        is True,
        "强制可信路由不能因 API Key 内容检测可选语义跳过首段缓冲保护",
    )
    degraded_provider = _provider(
        content_guard_enabled=True,
        trust_level="standard",
        content_integrity_status="degraded",
        health_status="healthy",
        buffer_stream_for_guard=False,
    )
    _assert(
        ContentRuntimeGuardService.stream_should_buffer(
            setting=setting_enabled,
            provider=degraded_provider,
            route_context=_context(),
        ) is True,
        "内容完整性 degraded 的标准 provider 必须在 buffer_300ms/full_buffer 模式下强制首段缓冲",
    )
    unknown_health_provider = _provider(
        content_guard_enabled=True,
        trust_level="official",
        content_integrity_status="passed",
        health_status="unknown",
        buffer_stream_for_guard=False,
    )
    _assert(
        ContentRuntimeGuardService.stream_should_buffer(
            setting=setting_enabled,
            provider=unknown_health_provider,
            route_context=_context(),
        ) is True,
        "健康状态 unknown 的 provider 不能因自定义关闭缓冲而跳过首段防护",
    )
    runtime_provider = _provider(content_integrity_status="unknown", circuit_state="closed")
    runtime_model = ProviderModel(
        id=4501,
        provider_id=runtime_provider.id,
        model_name="stage33-runtime-pass-model",
        enabled=True,
        content_integrity_status="degraded",
        content_probe_results_json='{"status":"degraded"}',
    )
    runtime_provider.provider_models = [runtime_model]
    ContentRuntimeGuardService.record_runtime_pass(
        db=None,
        provider=runtime_provider,
        provider_model=runtime_model,
    )
    _assert(runtime_model.content_probe_last_passed_at is None, "正式请求通过不得写可信探针通过时间")
    _assert(runtime_model.content_integrity_status == "degraded", "正式请求通过不得冒充可信探针恢复内容完整性状态")
    _assert(runtime_model.content_probe_results_json == '{"status":"degraded"}', "正式请求通过不得覆盖可信探针结果 JSON")
    high_result = ContentGuardResult(
        result=ContentGuardRuleService.RESULT_BLOCK,
        risk_level="high",
        categories=["unexpected_link"],
        reason="高风险污染",
        action="block",
    )
    safe_setting = type(
        "Setting",
        (),
        {
            "content_guard_enabled": True,
            "content_guard_high_risk_strategy": "safe_error",
            "content_guard_block_on_high_risk": True,
        },
    )()
    _assert(
        ContentRuntimeGuardService.should_emit_safe_error(high_result, setting=safe_setting) is True,
        "safe_error 策略必须有独立运行时判定",
    )
    safe_error_payload = ContentRuntimeGuardService.build_guard_error(
        guard_result=high_result,
        trace_id="stage33-safe-error",
        retried=False,
    )
    _assert(
        safe_error_payload["error"]["code"] == "content_integrity_safe_error",
        f"safe_error 必须返回独立错误码，避免与普通阻断混同：{safe_error_payload}",
    )


def _check_content_guard_record_violation_semantics() -> None:
    provider = _provider(
        id=3311,
        name="stage33-运行时违规记录提供商",
        content_integrity_status="passed",
        content_integrity_score=80,
        circuit_state="closed",
    )
    provider_model = ProviderModel(
        id=4411,
        provider_id=provider.id,
        model_name="stage33-runtime-guard-model",
        enabled=True,
        content_integrity_status="passed",
        circuit_state="closed",
    )
    db = _FakeProviderSession(provider)
    review_result = ContentGuardResult(
        result=ContentGuardService.RESULT_REVIEW,
        risk_level="medium",
        categories=["unexpected_link"],
        reason="待复核外链",
        action="record",
        score_delta=-8,
    )
    ContentGuardService.record_violation(db, provider=provider, provider_model=provider_model, result=review_result, auto_commit=False)
    _assert(provider.content_integrity_status == "passed", "review/record_only 记录不得污染 provider 内容完整性状态")
    _assert(provider.content_integrity_score == 80, "review/record_only 记录不得扣减 provider 内容完整性评分")
    _assert(provider_model.content_integrity_status == "passed", "review/record_only 记录不得污染 model 内容完整性状态")
    _assert(not db.added, "review 记录不得写高风险告警事件")

    record_only_result = ContentGuardResult(
        result=ContentGuardService.RESULT_BLOCK,
        risk_level="high",
        categories=["unexpected_link"],
        reason="高风险仅记录",
        action="record",
        final_strategy="record_only",
        score_delta=-25,
    )
    ContentGuardService.record_violation(db, provider=provider, provider_model=provider_model, result=record_only_result, auto_commit=False)
    _assert(provider.content_integrity_status == "passed", "record_only 高风险记录不得隔离 provider")
    _assert(provider_model.content_integrity_status == "passed", "record_only 高风险记录不得隔离 model")
    _assert(provider.circuit_state == "closed" and provider_model.circuit_state == "closed", "record_only 高风险记录不得打开熔断")

    block_result = ContentGuardResult(
        result=ContentGuardService.RESULT_BLOCK,
        risk_level="high",
        categories=["unexpected_link"],
        reason="高风险外链污染",
        action="block",
        score_delta=-25,
    )
    ContentGuardService.record_violation(db, provider=provider, provider_model=provider_model, result=block_result, auto_commit=False)
    _assert(provider.content_integrity_status == "blocked", "高风险命中必须隔离 provider")
    _assert(provider.circuit_state == "open", "高风险命中必须打开 provider 熔断")
    _assert(provider.circuit_opened_at is not None, "高风险命中必须记录 provider 熔断开启时间")
    _assert(provider_model.content_integrity_status == "blocked", "高风险命中必须隔离 model")
    _assert(provider_model.circuit_state == "open", "高风险命中必须打开 model 熔断")
    _assert(provider_model.circuit_opened_at is not None, "高风险命中必须记录 model 熔断开启时间")
    _assert(any(isinstance(item, AlertEvent) for item in db.added), "高风险运行时隔离必须写入统一 alert_events")


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
    proxy_text = Path("app/services/proxy_service.py").read_text(encoding="utf-8", errors="ignore")
    _assert(
        proxy_text.count("route_context = ProxyService._with_content_guard_route_policy(") >= 2,
        "正式非流式与流式代理链路都必须接入内容防护可信路由策略",
    )


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
    recent_unknown_model = ProviderModel(
        id=4406,
        provider_id=provider.id,
        model_name="stage33-recent-unknown-content-model",
        enabled=True,
        content_integrity_status="unknown",
        content_probe_last_failed_at=datetime.utcnow() - timedelta(seconds=60),
    )
    _assert(
        HealthService._should_run_scheduled_content_probe(provider, recent_unknown_model, setting=setting) is False,
        "unknown 模型已有近期检测结果时必须继续尊重配置间隔",
    )
    stale_degraded_model = ProviderModel(
        id=4407,
        provider_id=provider.id,
        model_name="stage33-stale-degraded-content-model",
        enabled=True,
        content_integrity_status="degraded",
        content_probe_last_failed_at=datetime.utcnow() - timedelta(seconds=301),
    )
    _assert(
        HealthService._should_run_scheduled_content_probe(provider, stale_degraded_model, setting=setting) is True,
        "degraded 模型必须在检测间隔到期后再进入自动预检",
    )

    original_get_cached = app_tasks.SettingService.get_cached
    try:
        app_tasks.SettingService.get_cached = staticmethod(lambda: type("Setting", (), {"content_guard_probe_interval_sec": 900})())
        _assert(app_tasks._content_integrity_lock_ttl_seconds() == 1800, "内容预检任务锁 TTL 必须随检测间隔动态计算")
    finally:
        app_tasks.SettingService.get_cached = original_get_cached
    tasks_text = Path("app/tasks.py").read_text(encoding="utf-8", errors="ignore")
    _assert(
        "content_guard_probe_interval = max(" in tasks_text
        and "seconds=content_guard_probe_interval" in tasks_text
        and "ttl_seconds=_content_integrity_lock_ttl_seconds" in tasks_text,
        "自动预检调度间隔和分布式锁 TTL 必须同时来自内容防护检测间隔配置",
    )


def _check_content_guard_retry_probe_semantics() -> None:
    trace = [
        {"result": "content_integrity_violation", "provider_id": 1, "provider_model_id": 10},
        {"result": "content_integrity_violation", "provider_id": 1, "provider_model_id": 11},
        {"result": "success", "provider_id": 1, "provider_model_id": 12},
    ]
    _assert(
        ProxyService._content_guard_retry_provider_count(trace) == 2,
        "内容防护重试次数必须按真实拦截尝试计数，不能只按 provider 去重",
    )
    success = ContentGuardProbeService.probe_success(
        endpoint_path="/responses",
        endpoint_label="stage33 成功探针",
        support_label="stage33 成功",
        latency_ms=1,
        status_code=200,
        message="ok",
        trace=[{"fallback_from": "/responses", "fallback_to": "/chat/completions"}],
    )
    _assert(success["adapted_success"] is True and success["support_mode"] == "adapted", "探针成功结果必须反映端点回退或协议适配")
    failure = ContentGuardProbeService.probe_failure(
        endpoint_path="/responses",
        endpoint_label="stage33 失败探针",
        support_label="内容检测失败",
        latency_ms=1,
        status_code=200,
        guard_result=ContentGuardProbeService.review_result("污染失败", category="stage33_probe_failed"),
    )
    _assert(failure["support_mode"] == "content_guard_failed", "内容污染失败禁止标成能力 unsupported")
    _assert(isinstance(failure.get("content_guard"), dict), "探针失败必须携带 content_guard 供可信状态聚合")

    probe_text = Path("app/services/content_guard_probe_service.py").read_text(encoding="utf-8", errors="ignore")
    proxy_text = Path("app/services/proxy_service.py").read_text(encoding="utf-8", errors="ignore")
    metrics_text = Path("app/services/system_metrics_service.py").read_text(encoding="utf-8", errors="ignore")
    content_guard_block_handler = proxy_text.split("except ContentGuardBlockedError as exc:", 1)[1].split("except HTTPException:", 1)[0]
    _assert("LogService.create_log" not in content_guard_block_handler, "切换提供商的中间内容防护拦截禁止写独立失败 request_logs")
    stream_prefetch_block = proxy_text.split("if ProxyService._content_guard_should_block_for_response(prefetch_guard_result, setting=setting):", 1)[1].split("async def stream_generator", 1)[0]
    _assert(
        '"content_integrity_violation"' in stream_prefetch_block
        and "trace.append(" in stream_prefetch_block
        and "last_upstream_error = ProxyService._content_guard_upstream_error" in stream_prefetch_block
        and "LogService.create_log" not in stream_prefetch_block,
        "流式首段预检切换提供商必须只写 trace 并延续同一客户端请求日志，禁止中间候选写独立失败 request_logs",
    )
    stream_chunk_block = proxy_text.split("if ProxyService._content_guard_should_block_for_response(current_guard_result, setting=setting):", 1)[1].split("if first_chunk_latency_ms is None:", 1)[0]
    _assert(
        '"content_integrity_violation"' in stream_chunk_block
        and "trace.append(" in stream_chunk_block
        and "ProxyService._record_content_guard_violation_by_id" in stream_chunk_block,
        "流式分块命中内容防护必须写 content_integrity_violation 提供商尝试 trace，并单独记录内容防护事件",
    )
    _assert(
        "error_type=OpenAIErrorService.classify_error(" in proxy_text
        and 'detail={"code": "content_integrity_violation"}' in proxy_text
        and ')["error_type"]' in proxy_text,
        "内容防护违规日志必须从统一错误目录解析 error_type，禁止固定 server_error",
    )
    _assert(
        ErrorCatalogService.resolve(
            status_code=ContentRuntimeGuardService.CONTENT_GUARD_ERROR_STATUS_CODE,
            detail={"code": "content_integrity_violation"},
        ).error_type
        == "content_policy_error",
        "内容防护违规错误目录禁止归类为 server_error",
    )
    _assert(
        ErrorCatalogService.resolve(
            status_code=ContentRuntimeGuardService.CONTENT_GUARD_ERROR_STATUS_CODE,
            detail={"code": "content_integrity_safe_error"},
        ).error_type
        == "content_policy_error",
        "safe_error 内容防护错误目录禁止归类为 server_error",
    )
    metric_body = metrics_text.split("def _content_guard_snapshot", 1)[1].split("def _empty_content_guard", 1)[0]
    _assert("RequestContentGuardEvent" in metric_body and "RequestLog.content_guard_result" not in metric_body, "监控内容防护高风险统计必须基于内容防护事件而非 request_logs 尝试次数")
    _assert('"support_mode": "probe_failed"' in probe_text and '"content_guard": ContentGuardProbeService.serialize_guard_result(guard_result)' in probe_text, "HTTP/网络失败探针必须带 content_guard 聚合字段")
    _assert("partial_stream" in probe_text and "stream_done_seen" in probe_text, "SSE 长流未到 DONE 时必须记录为部分长流结果，避免误判污染失败")
    _assert("fallback_trace = ContentGuardProbeService.mark_detection_trace" in probe_text and "trace=fallback_trace" in probe_text, "SSE 探针结果必须保留端点回退或适配 trace")
    _assert("stop_at_first_event=False" in proxy_text, "buffer_300ms 首段流式预检必须按字节或时间阈值读取，禁止首个 SSE 事件即停止")
    _assert("trim_stream_window(event_buffer" in Path("app/services/content_runtime_guard_service.py").read_text(encoding="utf-8", errors="ignore"), "流式分块检测必须保留滑动尾窗，禁止达到阈值后清空缓冲")


async def _check_proxy_db_write_transaction_boundary() -> None:
    class _TxDb:
        def __init__(self) -> None:
            self.commit_count = 0
            self.rollback_count = 0
            self.value = None

        def commit(self) -> None:
            self.commit_count += 1

        def rollback(self) -> None:
            self.rollback_count += 1

    def write_value(db, value):
        db.value = value
        return "ok"

    db = _TxDb()
    result = await ProxyService._run_db_write(write_value, "stage33", db=db)
    _assert(result == "ok" and db.value == "stage33", "复用外部 db 的写操作必须返回原始结果")
    _assert(db.commit_count == 1, f"复用外部 db 的写操作成功后必须提交事务：{db.commit_count}")

    def fail_write(db):
        db.value = "failed"
        raise ValueError("stage33 failure")

    try:
        await ProxyService._run_db_write(fail_write, db=db)
    except ValueError:
        pass
    else:
        raise AssertionError("复用外部 db 的写操作异常必须继续抛出")
    _assert(db.rollback_count == 1, f"复用外部 db 的写操作异常后必须回滚事务：{db.rollback_count}")


async def _check_stream_guard_pending_prefetch_before_downstream() -> None:
    async def _chunks():
        yield 'data: {"choices":[{"delta":{"content":"扫码加入博彩返利群"}}]}\n\n'.encode("utf-8")

    setting = type(
        "Setting",
        (),
        {
            "content_guard_enabled": True,
            "content_guard_stream_mode": "buffer_300ms",
            "content_guard_high_risk_strategy": "switch_provider",
            "content_guard_block_on_high_risk": True,
            "content_guard_rules_json": "",
            "content_guard_url_allowlist_json": "[]",
            "content_guard_url_check_enabled": True,
            "content_guard_max_scan_bytes": 16384,
            "stream_first_token_timeout_seconds": 1,
            "stream_idle_timeout_seconds": 1,
            "stream_max_duration_seconds": 5,
        },
    )()
    provider = _provider(
        content_guard_enabled=True,
        content_integrity_status="unknown",
        first_token_timeout_sec=1,
    )
    provider_model = ProviderModel(
        id=5104,
        provider_id=provider.id,
        model_name="stage33-stream-pending-prefetch",
        enabled=True,
        content_integrity_status="unknown",
    )
    iterator = _chunks().__aiter__()
    pending_task = asyncio.create_task(iterator.__anext__())
    inspected: dict[str, Any] = {}
    original_inspect = ProxyService._inspect_stream_guard_buffer

    async def fake_inspect_stream_guard_buffer(**kwargs):
        inspected["buffered_bytes"] = kwargs.get("buffered_bytes")
        return ContentGuardResult(
            result=ContentGuardRuleService.RESULT_BLOCK,
            risk_level="high",
            categories=["stage33_pending_prefetch"],
            reason="pending 首块污染",
            action="block",
            final_strategy="switch_provider",
        )

    try:
        ProxyService._inspect_stream_guard_buffer = staticmethod(fake_inspect_stream_guard_buffer)
        (
            prefetched_bytes,
            guard_result,
            first_chunk_latency_ms,
            stream_ended,
            returned_pending_task,
        ) = await ProxyService._resolve_pending_stream_guard_prefetch_before_downstream(
            db=None,
            setting=setting,
            provider=provider,
            provider_model=provider_model,
            endpoint_path="/v1/chat/completions",
            request_payload={"messages": [{"role": "user", "content": "正常回答"}]},
            chunk_iterator=iterator,
            pending_read_task=pending_task,
            started=time.perf_counter(),
            limit_bytes=16384,
            route_context=_context(),
        )
    finally:
        ProxyService._inspect_stream_guard_buffer = original_inspect
    _assert(prefetched_bytes, "首段预检超时后的 pending 首块必须在返回下游前被读取")
    _assert(inspected.get("buffered_bytes") == prefetched_bytes, "pending 首块必须在返回流式生成器前送入首段防护检测")
    _assert(guard_result.result == ContentGuardRuleService.RESULT_BLOCK, f"pending 首块命中污染时必须仍处于可切换前阻断：{guard_result}")
    _assert(first_chunk_latency_ms is not None, "pending 首块预检必须保留首 Token 延迟")
    _assert(stream_ended is False and returned_pending_task is None, "pending 首块解析完成后不得把未检测任务交给下游生成器")


def _check_content_guard_frontend_policy_wiring() -> None:
    app_js = Path("app/static/js/app.js").read_text(encoding="utf-8")
    api_keys_template = Path("app/templates/api_keys.html").read_text(encoding="utf-8")
    user_template = Path("app/templates/user_api_keys.html").read_text(encoding="utf-8")
    user_router = Path("app/routers/user_portal.py").read_text(encoding="utf-8")
    module_service_text = Path("app/services/content_guard_module_service.py").read_text(encoding="utf-8")
    _assert("selectedProviderId = providerSelect.value" in app_js, "内容防护概览刷新必须保留提供商选择")
    _assert("selectedModelId = providerModelSelect.value" in app_js, "内容防护概览刷新必须保留模型选择")
    _assert("existingChecked" in app_js and "selectedKeys" in app_js, "内容防护概览刷新必须保留探针勾选状态")
    _assert('"/api/content-guard/precheck/trust-probe"' in app_js, "内容防护页面必须调用分层后的预先防护可信探针接口")
    _assert('"/api/content-guard/precheck/probe"' not in app_js and '"/api/content-guard/probe"' not in app_js, "内容防护页面禁止继续调用旧预检探针接口")
    _assert("await loadOverview({ throwOnError: true });" in app_js, "内容防护设置保存后必须先刷新概览成功再反馈成功")
    _assert("applySettings(response.settings || {})" not in app_js, "内容防护设置保存禁止在概览刷新前直接套用响应设置造成混合状态")
    _assert('error: "检测异常"' in app_js, "内容防护前端结果映射必须覆盖 error")
    _assert('statusValue === "review" || statusValue === "error"' in app_js, "内容防护 error 结果必须使用异常样式")
    _assert("provider_filters = [enabled_provider_filter]" in module_service_text, "内容防护概览必须只查询已启用提供商")
    _assert("if item.enabled" in module_service_text, "内容防护概览模型列表必须只返回已启用模型")
    _assert("api-key-content-guard-filter" not in api_keys_template, "API Key 管理端禁止继续暴露内容检测局部筛选")
    _assert("api-key-template-content-guard-required" not in api_keys_template, "API Key 策略模板禁止继续暴露内容检测局部控件")
    _assert("content_guard_required: templateContentGuardRequiredInput.checked" not in app_js, "策略模板提交禁止继续写入内容检测局部字段")
    _assert("contentGuardRequiredInput.checked = template.content_guard_required" not in app_js, "套用模板禁止继续预填内容检测局部字段")
    _assert("user-api-key-create-content-guard-required" not in user_template, "普通用户端禁止提供关闭内容检测的创建控件")
    _assert('name="content_guard_required"' not in user_template, "普通用户端禁止提交内容检测开关字段")
    _assert("content_guard_required: str | None = Form" not in user_router, "普通用户端接口禁止接收内容检测开关字段")
    _assert('"content_guard_required": True' not in user_router, "普通用户创建/更新 API Key 禁止继续写入内容检测局部字段")


def _check_content_guard_schema_and_audit_contracts() -> None:
    _expect_validation_error(
        lambda: ContentGuardTextInspectRequest(text="x" * (CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT + 1)),
        "文本检测接口必须限制 text 最大长度",
    )
    _expect_validation_error(
        lambda: ContentGuardTextInspectRequest(
            text="安全文本",
            request_payload={"messages": [{"role": "user", "content": "x" * (CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT + 1)}]},
        ),
        "文本检测接口必须限制 request_payload 最大字节数",
    )
    deep_request_payload: dict[str, object] = {"value": "底部"}
    for _ in range(13):
        deep_request_payload = {"next": deep_request_payload}
    _expect_validation_error(
        lambda: ContentGuardTextInspectRequest(text="安全文本", request_payload=deep_request_payload),
        "文本检测接口必须限制 request_payload 嵌套深度",
    )
    _expect_validation_error(
        lambda: ContentGuardTextInspectRequest(
            text="安全文本",
            request_payload={"items": [{"value": index} for index in range(2001)]},
        ),
        "文本检测接口必须限制 request_payload 结构节点数",
    )
    audit_detail = content_guard_router._content_guard_rules_audit_detail(
        {
            "stage33_rule": {
                "name": "旧规则",
                "category": "custom",
                "enabled": True,
                "match_type": "keyword_any",
                "risk_level": "medium",
                "action": "record",
                "score_delta": -8,
                "confidence": 0.7,
                "reason": "",
                "patterns": ["旧词"],
            }
        },
        {
            "stage33_rule": {
                "name": "新规则",
                "category": "custom",
                "enabled": True,
                "match_type": "keyword_any",
                "risk_level": "high",
                "action": "block",
                "score_delta": -30,
                "confidence": 0.95,
                "reason": "命中后直接阻断",
                "patterns": ["新词", "补充词"],
            }
        },
    )
    changed = audit_detail["changed_rule_diffs"][0]
    _assert(changed["rule_id"] == "stage33_rule", f"规则审计必须记录变更规则 ID：{changed}")
    _assert(changed["fields"]["action"] == {"before": "record", "after": "block"}, f"规则审计必须记录动作差异：{changed}")
    _assert(changed["fields"]["risk_level"] == {"before": "medium", "after": "high"}, f"规则审计必须记录风险等级差异：{changed}")
    _assert(changed["fields"]["patterns"]["before_sample"] == ["旧词"], f"规则审计必须记录匹配项样本：{changed}")


def _check_error_catalog() -> None:
    spec = ErrorCatalogService.resolve(status_code=502, detail={"code": "content_integrity_violation"})
    _assert(spec.code == "content_integrity_violation", "内容完整性错误码必须进入统一错误目录")
    safe_spec = ErrorCatalogService.resolve(status_code=502, detail={"code": "content_integrity_safe_error"})
    _assert(safe_spec.code == "content_integrity_safe_error", "safe_error 内容完整性错误码必须进入统一错误目录")
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
    _assert(stream_result.result == ContentGuardService.RESULT_REVIEW, f"健康检查流式探针应将非 JSON SSE 降级复核：{stream_result}")
    _assert(stream_result.risk_level == "medium", f"健康检查非 JSON SSE 应标记中风险：{stream_result}")
    stream_delta = ContentGuardProbeService.extract_probe_sse_text_delta(
        '{"choices":[{"delta":{"content":"AOTU_CONTENT_GUARD_OK"}}]}'
    )
    _assert(stream_delta == ContentGuardProbeService.FIXED_ANSWER, f"SSE 探针必须能聚合流式文本：{stream_delta}")

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
    _assert(failure["content_guard"]["content_guard_result"] == ContentGuardService.RESULT_REVIEW, f"探针失败应保留非 JSON SSE 复核上下文：{failure}")
    _assert(failure["content_guard"]["content_guard_risk_level"] == "medium", f"探针失败应保留中风险上下文：{failure}")

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
    _assert(incomplete_decision["content_guard_result"] == "block", f"缺少必需可信探针必须明确不具备可信资格：{incomplete_decision}")
    _assert(HealthService._content_probe_exception_retryable(ValueError("配置错误")) is False, "本地配置异常不得标记为可重试")
    _assert(HealthService._content_probe_exception_retryable(httpx.TimeoutException("timeout")) is True, "网络超时应保留可重试语义")

    records: list[dict[str, Any]] = []
    original_record_probe = health_service_module.HealthLogRecorder.record_probe

    class _FakeHealthRecordDb:
        def __init__(self) -> None:
            self.commit_count = 0

        def commit(self) -> None:
            self.commit_count += 1

    def fake_record_probe(db, **kwargs):
        records.append(kwargs)

    try:
        health_service_module.HealthLogRecorder.record_probe = staticmethod(fake_record_probe)
        fake_db = _FakeHealthRecordDb()
        HealthService._record_run_results(
            fake_db,
            run_id="stage33-health-run",
            provider_results=[
                {
                    "provider_id": 1,
                    "model_results": [
                        {
                            "model_name": "stage33-model",
                            "success": False,
                            "endpoint_results": [
                                {
                                    "provider_model_id": 2,
                                    "capability_key": "content_trust_probe",
                                    "endpoint_path": None,
                                    "success": False,
                                    "support_mode": "error",
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    finally:
        health_service_module.HealthLogRecorder.record_probe = original_record_probe
    _assert(records and records[0]["protocol_type"] is None, f"endpoint_path=None 的健康探针不得误记 responses 协议：{records}")


async def _check_scheduled_content_probe_batch_policy() -> None:
    setting = type("Setting", (), {"content_guard_probe_interval_sec": 300})()
    provider_no_due = _provider(id=4701, name="stage33-无到期模型提供商")
    provider_no_due.provider_models = [
        ProviderModel(
            id=4702,
            provider_id=provider_no_due.id,
            model_name="stage33-recent-model",
            enabled=True,
            content_integrity_status="passed",
            content_probe_last_passed_at=datetime.utcnow(),
        )
    ]
    no_due_results = await HealthService._run_scheduled_content_trust_probes(
        None,
        [provider_no_due],
        setting=setting,
    )
    _assert(no_due_results[0]["success"] is True, f"没有到期模型不得污染 provider 成功状态：{no_due_results}")
    _assert(no_due_results[0]["skipped"] is True and no_due_results[0]["status_code"] == 204, f"无到期模型应记录为跳过：{no_due_results}")

    provider = _provider(id=4710, name="stage33-并发预算提供商")
    provider.provider_models = [
        ProviderModel(
            id=4800 + index,
            provider_id=provider.id,
            model_name=f"stage33-budget-model-{index}",
            enabled=True,
            content_integrity_status="unknown",
        )
        for index in range(HealthService.SCHEDULED_CONTENT_INTEGRITY_MODEL_LIMIT + 5)
    ]
    original_probe = HealthService._run_scheduled_content_trust_probe_for_model
    active = 0
    peak = 0
    called: list[int] = []

    async def fake_probe(provider_id: int, provider_model_id: int) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        called.append(provider_model_id)
        return {
            "provider_model_id": provider_model_id,
            "model_name": f"stage33-budget-model-{provider_model_id}",
            "success": True,
            "provider_success": True,
            "health_status": "healthy",
            "content_integrity_status": "passed",
            "latency_ms": 1,
            "status_code": 200,
            "message": "ok",
            "endpoint_results": [],
        }

    try:
        HealthService._run_scheduled_content_trust_probe_for_model = staticmethod(fake_probe)
        budget_results = await HealthService._run_scheduled_content_trust_probes(
            None,
            [provider],
            setting=setting,
        )
    finally:
        HealthService._run_scheduled_content_trust_probe_for_model = original_probe
    _assert(len(called) == HealthService.SCHEDULED_CONTENT_INTEGRITY_MODEL_LIMIT, f"单轮内容预检必须受最大模型预算限制：{len(called)}")
    _assert(1 < peak <= HealthService.SCHEDULED_CONTENT_INTEGRITY_CONCURRENCY, f"内容预检必须有界并发执行：peak={peak}")
    _assert(budget_results[0]["models_total"] == HealthService.SCHEDULED_CONTENT_INTEGRITY_MODEL_LIMIT, f"provider 汇总必须反映预算后的模型数：{budget_results}")


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
    pass_guard_result = {
        "content_guard_result": ContentGuardService.RESULT_PASS,
        "content_guard_reason": "探针通过",
        "content_guard_action": "allow",
    }
    endpoint_results = [
        {
            "capability_key": "content_fixed_answer",
            "endpoint_label": "固定答案完整性探针",
            "success": False,
            "content_guard": review_result,
        },
        {
            "capability_key": "content_pollution_rules",
            "endpoint_label": "外链广告识别探针",
            "success": True,
            "content_guard": pass_guard_result,
        },
        {
            "capability_key": "content_sse",
            "endpoint_label": "流式污染检测探针",
            "success": True,
            "content_guard": pass_guard_result,
        },
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
    block_result = {
        "content_guard_result": ContentGuardService.RESULT_BLOCK,
        "content_guard_reason": "单次高风险探针失败",
        "content_guard_action": "block",
    }
    provider_model.content_probe_last_failed_at = datetime.utcnow() - timedelta(minutes=11)
    provider_model.content_probe_failure_count = 2
    ContentGuardProbeService.apply_content_probe_health(
        db,
        provider,
        provider_model,
        content_guard_result=block_result,
        endpoint_results=[
            {
                "capability_key": "content_fixed_answer",
                "endpoint_label": "固定答案完整性探针",
                "success": True,
                "content_guard": pass_guard_result,
            },
            {
                "capability_key": "content_pollution_rules",
                "endpoint_label": "外链广告识别探针",
                "success": False,
                "content_guard": block_result,
            },
            {
                "capability_key": "content_sse",
                "endpoint_label": "流式污染检测探针",
                "success": True,
                "content_guard": pass_guard_result,
            },
        ],
    )
    _assert(provider_model.content_probe_failure_count == 1, "窗口外单次高风险失败不得沿用旧失败次数")
    _assert(provider_model.content_integrity_status == "degraded", "非缺项的单次高风险探针失败不得绕过 10 分钟窗口直接 blocked")

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
    serialized = loads_json(provider_model.content_probe_results_json, {})
    _assert(serialized.get("status") == "passed", f"探针序列化状态必须写最终模型状态：{serialized}")
    _assert(serialized.get("provider_status") == "passed", f"探针序列化必须记录最终 provider 状态：{serialized}")


def _check_trust_probe_keys_and_probe_boundaries() -> None:
    merged = ContentTrustProbeService.merge_required_probe_keys(["json", "fixed_answer"])
    _assert(merged == ["json", "fixed_answer", "pollution_rules", "sse"], f"可信探针必须保留用户勾选项并补齐必需项：{merged}")
    invalid = ContentTrustProbeService.invalid_probe(
        probe_key="frontend_unknown",
        endpoint_path="/responses",
        message="未知探针",
    )
    _assert(invalid["support_mode"] == "invalid_probe", f"未知探针必须显式失败而不是 skipped：{invalid}")
    _assert(invalid["success"] is False and invalid.get("content_guard"), f"未知探针失败必须带内容防护上下文：{invalid}")

    async def _run_optional_persist_boundary() -> None:
        provider = Provider(id=4501, name="阶段三十三能力探针提供商", enabled=True)
        provider_model = ProviderModel(
            id=4502,
            provider_id=4501,
            model_name="阶段三十三能力探针模型",
            enabled=True,
            supports_stream=True,
        )
        updates: list[list[str]] = []
        original_resolve_target = ContentTrustProbeService._resolve_probe_target
        original_resolve_endpoint = ContentTrustProbeService._resolve_endpoint_path
        original_run_single_probe = ContentTrustProbeService.run_single_probe
        original_run_combined_probe = ContentTrustProbeService.run_combined_text_probe_with_boundary
        original_update_status = ContentTrustProbeService.update_provider_model_trust_status
        original_json_probe_enabled = ContentTrustProbeService.json_probe_enabled
        running_probe_count = 0
        max_parallel_probe_count = 0

        def fake_resolve_target(db, payload):
            return provider, provider_model, {"provider_id": provider.id, "provider_model_id": provider_model.id}

        def fake_resolve_endpoint(provider_arg, provider_model_arg, payload):
            return "/responses"

        async def fake_run_single_probe(provider_arg, provider_model_arg, endpoint_path, probe_key):
            nonlocal running_probe_count, max_parallel_probe_count
            running_probe_count += 1
            max_parallel_probe_count = max(max_parallel_probe_count, running_probe_count)
            await asyncio.sleep(0.01)
            running_probe_count -= 1
            return {
                "capability_key": probe_key,
                "endpoint_path": endpoint_path,
                "success": True,
                "content_guard": {"content_guard_result": ContentGuardService.RESULT_PASS},
            }

        async def fake_run_combined_probe(provider_arg, provider_model_arg, endpoint_path, *, order_indexes):
            return [
                (
                    order_indexes.get("fixed_answer", 0),
                    {
                        "capability_key": "fixed_answer",
                        "endpoint_path": endpoint_path,
                        "success": True,
                        "content_guard": {"content_guard_result": ContentGuardService.RESULT_PASS},
                    },
                ),
                (
                    order_indexes.get("pollution_rules", 1),
                    {
                        "capability_key": "pollution_rules",
                        "endpoint_path": endpoint_path,
                        "success": True,
                        "content_guard": {"content_guard_result": ContentGuardService.RESULT_PASS},
                    },
                ),
            ]

        def fake_update_status(db, provider_arg, provider_model_arg, *, content_guard_result, endpoint_results, detection_source):
            updates.append([item.get("capability_key") for item in endpoint_results])

        try:
            ContentTrustProbeService._resolve_probe_target = staticmethod(fake_resolve_target)
            ContentTrustProbeService._resolve_endpoint_path = staticmethod(fake_resolve_endpoint)
            ContentTrustProbeService.run_single_probe = staticmethod(fake_run_single_probe)
            ContentTrustProbeService.run_combined_text_probe_with_boundary = staticmethod(fake_run_combined_probe)
            ContentTrustProbeService.update_provider_model_trust_status = staticmethod(fake_update_status)
            ContentTrustProbeService.json_probe_enabled = staticmethod(lambda: True)
            await ContentTrustProbeService.run_capability_probe(
                None,
                ContentGuardRunRequest(
                    target_type="internal",
                    provider_id=provider.id,
                    provider_model_id=provider_model.id,
                    probe_keys=["json", "sse"],
                    persist_internal_result=True,
                ),
            )
            _assert(not updates, f"只选可选能力探针时禁止写入模型可信状态：{updates}")
            await ContentTrustProbeService.run_capability_probe(
                None,
                ContentGuardRunRequest(
                    target_type="internal",
                    provider_id=provider.id,
                    provider_model_id=provider_model.id,
                    probe_keys=["fixed_answer", "pollution_rules", "sse"],
                    persist_internal_result=True,
                ),
            )
            _assert(updates == [["fixed_answer", "pollution_rules", "sse"]], f"必需可信探针齐全时才允许写入可信状态：{updates}")
            _assert(max_parallel_probe_count > 1, "同一模型的多个预检能力探针必须并行执行以降低等待时间")
        finally:
            ContentTrustProbeService._resolve_probe_target = original_resolve_target
            ContentTrustProbeService._resolve_endpoint_path = original_resolve_endpoint
            ContentTrustProbeService.run_single_probe = original_run_single_probe
            ContentTrustProbeService.run_combined_text_probe_with_boundary = original_run_combined_probe
            ContentTrustProbeService.update_provider_model_trust_status = original_update_status
            ContentTrustProbeService.json_probe_enabled = original_json_probe_enabled

    asyncio.run(_run_optional_persist_boundary())

    template_text = Path("app/templates/content_guard.html").read_text(encoding="utf-8")
    app_js_text = Path("app/static/js/app.js").read_text(encoding="utf-8")
    router_text = Path("app/routers/content_guard.py").read_text(encoding="utf-8")
    _assert("能力探针与可信状态" in template_text and "能力探针类型" in template_text, "内容防护页面必须区分能力探针与可信状态写入语义")
    _assert("可选能力探针只记录本次结果" in app_js_text, "前端提示必须说明可选能力探针不会单独写入可信状态")
    _assert("manual_precheck_capability_probe" in router_text and "执行内容防护预检能力探针" in router_text, "预检能力探针审计语义不得与可信探针混用")

    service_text = Path("app/services/content_guard_probe_service.py").read_text(encoding="utf-8")
    trust_probe_text = Path("app/services/content_trust_probe_service.py").read_text(encoding="utf-8")
    _assert("SSE_PROBE_MAX_CHUNKS" in service_text and "SSE_PROBE_MAX_BYTES" in service_text, "SSE 探针必须有 chunk 与字节边界")
    _assert("range(6)" not in service_text, "SSE 探针禁止固定只读取 6 个 chunk")
    _assert("build_stream_pollution_probe_payload" in service_text, "SSE 探针必须覆盖普通长流式回答污染场景")
    _assert("POLLUTION_PROBE_TIMEOUT_SECONDS" in service_text, "外链广告识别组合探针必须有总超时")
    _assert("build_combined_pollution_probe_prompt" in service_text, "外链广告识别探针必须合并为单次上游请求")
    _assert("split_combined_pollution_output" in service_text, "外链广告识别组合探针必须拆分子场景结果")
    _assert("probe_pollution_scenario" not in service_text, "外链广告识别探针禁止回退为每个场景单独请求")
    _assert("asyncio.wait_for" in service_text and "pollution_probe_timeout" in service_text, "外链广告识别探针必须实际执行超时控制")
    _assert("run_single_probe_with_boundary" in trust_probe_text and "PROBE_TIMEOUT_SECONDS" in trust_probe_text, "预检能力探针必须有单探针边界与结构化异常结果")
    for scenario_key in ("long_context_answer", "tool_context_answer", "markdown_answer", "citation_answer"):
        _assert(scenario_key in service_text, f"外链广告识别探针缺少真实污染场景：{scenario_key}")
    _assert("build_pollution_probe_guard_request" in service_text, "外链广告识别探针检测上下文必须与真实 prompt 隔离，避免自白名单")
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
    isolation_payload = events["monitoring:content_guard_high_risk_provider:3302"]["payload"]
    _assert(changed is True, "10 分钟高风险 3 次必须触发自动隔离状态变更")
    _assert(fake_db.commit_count == 1, f"自动隔离应提交一次状态变更：{fake_db.commit_count}")
    _assert(isolation_payload.get("isolated_at"), f"首次自动隔离必须记录稳定隔离时间：{isolation_payload}")
    _assert(
        (isolation_payload.get("pre_isolation_snapshot") or {}).get("provider", {}).get("content_integrity_status") == "passed",
        f"自动隔离必须记录 provider 隔离前状态快照：{isolation_payload}",
    )
    _assert(
        (isolation_payload.get("pre_isolation_snapshot") or {}).get("models", [{}])[0].get("content_integrity_status") == "passed",
        f"自动隔离必须记录挂载模型隔离前状态快照：{isolation_payload}",
    )
    _assert(provider.trust_level == "standard", f"自动隔离不得改写提供商信任等级：{provider.trust_level}")
    _assert(provider.content_integrity_status == "blocked", f"自动隔离必须标记内容完整性 blocked：{provider.content_integrity_status}")
    _assert(provider.circuit_state == "open", f"自动隔离必须打开提供商熔断：{provider.circuit_state}")
    _assert(provider.circuit_opened_at is not None, "自动隔离必须记录 provider 级熔断打开时间")
    _assert(provider.content_integrity_score <= 20, f"自动隔离必须压低内容完整性分：{provider.content_integrity_score}")
    model = provider.provider_models[0]
    _assert(model.content_integrity_status == "blocked", f"自动隔离必须同步隔离挂载模型：{model.content_integrity_status}")
    _assert(model.circuit_state == "open", f"自动隔离必须同步熔断挂载模型：{model.circuit_state}")
    _assert(isolation_payload["auto_isolated"] is True, "告警 payload 必须标记已自动隔离")

    isolated_at = isolation_payload["isolated_at"]
    changed_again = SystemMetricsService.apply_monitoring_alert_actions(fake_db, events)
    _assert(changed_again is False, "重复刷新已隔离提供商不应反复提交状态变更")
    _assert(isolation_payload["isolated_at"] == isolated_at, f"重复隔离不得刷新 isolated_at：{isolation_payload}")


def _check_monitoring_alert_write_stability() -> None:
    now = datetime.utcnow()
    alert_key = "monitoring:content_guard_high_risk_provider:3399"
    stable_payload = {
        "provider_id": 3399,
        "provider_name": "stage33-稳定告警提供商",
        "high_risk_count": 3,
        "window_minutes": 10,
        "auto_isolated": True,
        "isolation_status": "blocked",
        "isolated_at": "2026-06-09T00:00:00",
        "pre_isolation_snapshot": {"provider": {"content_integrity_status": "passed"}, "models": []},
    }
    existing = AlertEvent(
        alert_key=alert_key,
        alert_type="content_guard_high_risk_provider",
        severity="danger",
        title="内容高风险提供商自动隔离",
        message="稳定告警",
        payload_json=dumps_json(stable_payload),
        status="active",
        first_seen_at=now,
        last_seen_at=now,
    )
    active_events = {
        alert_key: {
            "alert_type": existing.alert_type,
            "severity": existing.severity,
            "title": existing.title,
            "message": existing.message,
            "payload": {
                key: value
                for key, value in stable_payload.items()
                if key not in {"isolated_at", "pre_isolation_snapshot"}
            },
        }
    }

    class _StableAlertDb:
        def __init__(self) -> None:
            self.commit_count = 0
            self.added: list[object] = []

        def add(self, item) -> None:
            self.added.append(item)

        def commit(self) -> None:
            self.commit_count += 1

    originals = {
        "_build_monitoring_alert_events": SystemMetricsService._build_monitoring_alert_events,
        "apply_monitoring_alert_actions": SystemMetricsService.apply_monitoring_alert_actions,
        "_load_relevant_monitoring_alerts": SystemMetricsService._load_relevant_monitoring_alerts,
        "_resolve_stale_monitoring_alerts": SystemMetricsService._resolve_stale_monitoring_alerts,
    }
    try:
        SystemMetricsService._build_monitoring_alert_events = classmethod(lambda cls, metrics: active_events)
        SystemMetricsService.apply_monitoring_alert_actions = classmethod(lambda cls, db, active_events, auto_commit=False: False)
        SystemMetricsService._load_relevant_monitoring_alerts = classmethod(lambda cls, db, active_keys: [existing])
        SystemMetricsService._resolve_stale_monitoring_alerts = classmethod(lambda cls, db, active_keys, now: False)
        fake_db = _StableAlertDb()
        SystemMetricsService.write_monitoring_alerts(fake_db, {})
    finally:
        for name, value in originals.items():
            setattr(SystemMetricsService, name, value)
    _assert(fake_db.commit_count == 0, f"无变化 active alert 不应每轮提交数据库：{fake_db.commit_count}")
    persisted_payload = loads_json(existing.payload_json, {})
    _assert(persisted_payload.get("isolated_at") == stable_payload["isolated_at"], f"告警写入必须保留既有 isolated_at：{persisted_payload}")
    _assert("pre_isolation_snapshot" in persisted_payload, f"告警写入必须保留隔离前快照：{persisted_payload}")


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
    _expect_validation_error(lambda: target(base_url="https://example.com/" + "x" * 2049), "外部检测接口地址必须限制最大长度")
    provider, provider_model, resolved_target = ContentTrustProbeService.resolve_external_probe_target(target())
    _assert(provider.provider_type == "external_probe", "外部渠道检测必须使用独立 external_probe 类型，避免混入正式提供商边界")
    _assert(provider.id < 0 and provider_model.id < 0, "外部渠道检测目标必须使用稳定负数临时标识，避免伪造正式 id=0")
    _assert(provider_model.supports_stream is False, "外部渠道未声明能力时不得默认支持流式检测")
    _assert(provider_model.supports_tools is False, "外部渠道未声明能力时不得默认支持工具调用检测")
    _assert(resolved_target["type"] == "external" and resolved_target["endpoint_path"] == "/responses", "外部渠道目标摘要必须保留检测端点")


def _check_content_probe_target_boundaries() -> None:
    provider = _provider(id=4601, name="stage33-目标校验提供商", enabled=True)
    disabled_model = ProviderModel(
        id=4602,
        provider_id=provider.id,
        model_name="stage33-disabled-model",
        enabled=False,
        supports_chat_completions=True,
        supports_responses=False,
    )
    provider.provider_models = [disabled_model]
    session = _FakeProbeTargetSession(provider, disabled_model)
    payload = ContentGuardRunRequest(
        target_type="internal",
        provider_id=provider.id,
        provider_model_id=disabled_model.id,
        probe_keys=["fixed_answer"],
    )
    _expect_validation_error(
        lambda: ContentTrustProbeService._resolve_probe_target(session, payload),
        "手动探针指定 provider_model_id 时必须拒绝已停用模型",
    )
    manual_provider = _provider(id=4603)
    manual_model = ProviderModel(id=4604, provider_id=manual_provider.id, model_name="stage33-manual-model", enabled=True)
    manual_model.content_integrity_status = "passed"
    ProviderService._ensure_manual_content_probe_reason(manual_model)
    manual_results = loads_json(manual_model.content_probe_results_json, {})
    phase_keys = {item.get("phase_key") for item in manual_results.get("results", [])}
    _assert(
        {"content_fixed_answer", "content_pollution_rules", "content_sse"}.issubset(phase_keys),
        "管理员手动标记可信必须同时记录固定答案、外链广告识别和流式污染检测确认",
    )


def _check_content_guard_settings_validation() -> None:
    valid = ContentGuardSettingsUpdate(content_guard_url_allowlist_json='["https://www.example.com","docs.example.com"]')
    _assert(valid.content_guard_url_allowlist_json == '["example.com", "docs.example.com"]', "URL 白名单必须规范化为 JSON 域名数组")
    _expect_validation_error(
        lambda: ContentGuardSettingsUpdate(content_guard_max_scan_bytes=CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT + 1),
        "内容防护最大扫描字节必须有后端提交上限",
    )
    _expect_validation_error(
        lambda: ContentGuardSettingsUpdate(content_guard_stream_buffer_max_bytes=CONTENT_GUARD_STREAM_BUFFER_MAX_BYTES_LIMIT + 1),
        "内容防护流式缓冲字节必须有后端提交上限",
    )
    _expect_validation_error(
        lambda: ContentGuardSettingsUpdate(content_guard_probe_interval_sec=86401),
        "内容防护专用设置必须限制自动预检间隔最大值",
    )
    for bad_allowlist in (
        "example.com",
        '{"domain":"example.com"}',
        '["not a domain"]',
        '["http://"]',
        '[123]',
    ):
        _expect_validation_error(
            lambda bad_allowlist=bad_allowlist: ContentGuardSettingsUpdate(content_guard_url_allowlist_json=bad_allowlist),
            f"URL 白名单配置必须在提交时校验 JSON 与域名格式：{bad_allowlist}",
        )
    setting_payload = {
        "route_mode": "manual",
        "content_guard_block_on_high_risk": False,
        "content_guard_high_risk_strategy": "record_only",
    }
    _expect_validation_error(
        lambda: SettingUpdate(**setting_payload, content_guard_probe_interval_sec=86401),
        "通用设置入口必须限制自动预检间隔最大值",
    )
    _expect_validation_error(
        lambda: SettingUpdate(**setting_payload, content_guard_url_allowlist_json="x" * 10001),
        "通用设置入口必须限制 URL 白名单长度",
    )
    _expect_validation_error(
        lambda: ContentGuardRulesUpdate(
            rules=[
                {
                    "id": "bad_regex",
                    "name": "危险正则",
                    "category": "custom",
                    "match_type": "regex",
                    "patterns": ["(a+)+"],
                    "risk_level": "medium",
                    "action": "record",
                    "score_delta": -8,
                }
            ]
        ),
        "内容防护规则保存必须拒绝灾难性回溯正则",
    )
    _expect_validation_error(
        lambda: ContentGuardRulesUpdate(rules=[]),
        "内容防护规则保存禁止空规则列表",
    )
    oversized_setting = type("OversizedContentGuardSetting", (), {"content_guard_max_scan_bytes": CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT * 10})()
    _assert(
        ContentRuntimeGuardService.bounded_max_scan_bytes(oversized_setting) == CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT,
        "运行时扫描窗口必须再次夹紧旧库或内部调用传入的超大值",
    )
    proxy_text = Path("app/services/proxy_service.py").read_text(encoding="utf-8", errors="ignore")
    _assert(
        "CONTENT_GUARD_STREAM_BUFFER_MAX_BYTES_LIMIT" in proxy_text
        and "guard_buffer_limit = min(" in proxy_text,
        "代理流式预检缓冲必须在运行时再次夹紧上限",
    )


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
    invalidated_prefixes: list[str] = []
    original_get_or_create = content_guard_module_service.SettingService.get_or_create
    original_setting_invalidate = content_guard_module_service.SettingService.invalidate_runtime_cache
    original_provider_invalidate = content_guard_module_service.ProviderService.invalidate_provider_runtime_cache
    original_cache_invalidate = content_guard_module_service.CacheService.invalidate_prefix
    content_guard_module_service.SettingService.get_or_create = staticmethod(lambda db: setting)
    content_guard_module_service.SettingService.invalidate_runtime_cache = staticmethod(lambda: invalidated_prefixes.append("runtime-settings"))
    content_guard_module_service.ProviderService.invalidate_provider_runtime_cache = staticmethod(lambda: invalidated_prefixes.append("providers-runtime-service"))
    content_guard_module_service.CacheService.invalidate_prefix = staticmethod(lambda prefix: invalidated_prefixes.append(prefix))
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
        content_guard_module_service.SettingService.invalidate_runtime_cache = original_setting_invalidate
        content_guard_module_service.ProviderService.invalidate_provider_runtime_cache = original_provider_invalidate
        content_guard_module_service.CacheService.invalidate_prefix = original_cache_invalidate
    _assert(fake_db.commit_count == 1 and fake_db.refreshed is setting, "内容防护设置提交必须落库并刷新")
    for prefix in ("runtime-settings", "providers-runtime-service", "providers-runtime", "route-candidates", "v1-models"):
        _assert(prefix in invalidated_prefixes, f"内容防护设置提交必须同步失效运行时和路由相关缓存：{prefix}")
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
    for table_name in ("request_content_guard_events", "health_probe_events"):
        match = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table_name}\s*\((.*?)\);",
            migration,
            re.DOTALL,
        )
        _assert(match is not None, f"迁移必须显式创建 typed logging 表：{table_name}")
        _assert("request_log_id" in match.group(1), f"typed logging 表必须保留请求日志关联字段：{table_name}")
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
            page.locator("#content-guard-inspect-text").fill("这是一段本地检测文本")
            _assert(
                page.locator("#content-guard-probe-form").count() == 1
                and page.locator("#content-guard-inspect-form").count() == 1,
                "内容防护页面必须渲染能力探针和本地检测表单",
            )
            _assert(
                page.locator("[data-content-guard-target-type='external']").is_disabled()
                and page.locator("input[name='content_guard_target_type']").count() == 0
                and page.locator("#content-guard-external-api-key").count() == 0,
                "内容防护页面禁止让浏览器持有外部 API Key，外部提供商只能通过后端托管配置检测",
            )
        finally:
            browser.close()


def _check_content_guard_api_route_smoke() -> None:
    app = FastAPI()
    app.include_router(content_guard_router.router)
    app.dependency_overrides[get_db] = lambda: object()
    app.dependency_overrides[content_guard_router.require_admin_api_user] = lambda: type(
        "Stage33User",
        (),
        {"id": 3301, "username": "stage33"},
    )()

    original_build_overview = ContentGuardModuleService.build_overview
    original_inspect_text = ContentGuardModuleService.inspect_text
    original_create_log = content_guard_router.AdminAuditService.create_log
    calls: dict[str, object] = {}

    def fake_build_overview(db, *, provider_keyword="", provider_page=1, provider_page_size=50):
        calls["overview"] = {
            "provider_keyword": provider_keyword,
            "provider_page": provider_page,
            "provider_page_size": provider_page_size,
        }
        return {
            "settings": {},
            "summary": {
                "provider_count": 0,
                "provider_filtered_count": 0,
                "provider_page": provider_page,
                "provider_page_size": provider_page_size,
                "provider_total_pages": 1,
            },
            "providers": [],
            "probe_options": [],
        }

    def fake_inspect_text(db, payload):
        calls["inspect_text"] = payload
        return {
            "result": {"content_guard_result": "pass"},
            "risk_level": "low",
            "action": "allow",
            "confidence": 0.1,
            "score_delta": 0,
            "matched_rules": [],
        }

    def fake_create_log(*args, **kwargs):
        calls["audit"] = {"args_count": len(args), "summary": kwargs.get("summary")}
        return None

    ContentGuardModuleService.build_overview = staticmethod(fake_build_overview)
    ContentGuardModuleService.inspect_text = staticmethod(fake_inspect_text)
    content_guard_router.AdminAuditService.create_log = staticmethod(fake_create_log)
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/content-guard/overview",
                params={
                    "provider_keyword": "阶段33",
                    "provider_page": 2,
                    "provider_page_size": 20,
                },
            )
            _assert(response.status_code == 200, "内容防护 overview 路由必须可通过 TestClient 调用")
            _assert(
                calls.get("overview") == {
                    "provider_keyword": "阶段33",
                    "provider_page": 2,
                    "provider_page_size": 20,
                },
                "内容防护 overview 路由必须把服务端分页和搜索参数传给模块服务",
            )
            inspect_response = client.post(
                "/api/content-guard/runtime/inspect-text",
                json={
                    "text": "阶段33 本地检测文本",
                    "endpoint_path": "/chat/completions",
                    "request_payload": {"messages": [{"role": "user", "content": "阶段33"}]},
                    "max_scan_bytes": 4096,
                },
            )
            _assert(inspect_response.status_code == 200, "内容防护本地检测路由必须可通过 TestClient 调用")
            inspect_payload = calls.get("inspect_text")
            _assert(
                isinstance(inspect_payload, ContentGuardTextInspectRequest)
                and inspect_payload.endpoint_path == "/chat/completions"
                and inspect_payload.request_payload == {"messages": [{"role": "user", "content": "阶段33"}]}
                and inspect_payload.max_scan_bytes == 4096,
                f"内容防护本地检测路由必须把端点、请求体和扫描上限传给模块服务：{inspect_payload}",
            )
    finally:
        ContentGuardModuleService.build_overview = original_build_overview
        ContentGuardModuleService.inspect_text = original_inspect_text
        content_guard_router.AdminAuditService.create_log = original_create_log
        app.dependency_overrides.clear()


def _check_content_guard_template_dom_contract() -> None:
    template = Path("app/templates/content_guard.html").read_text(encoding="utf-8", errors="ignore")
    parser = _ContentGuardHtmlContractParser()
    parser.feed(template)
    required_ids = {
        "content-guard-refresh-btn",
        "content-guard-settings-form",
        "content-guard-probe-form",
        "content-guard-inspect-form",
        "content-guard-result-body",
        "content-guard-runtime-events-body",
    }
    _assert(required_ids.issubset(parser.ids), f"内容防护模板缺少可交互 DOM：{sorted(required_ids - parser.ids)}")
    _assert(
        parser.result_headers == [
            "探针",
            "端点",
            "结果",
            "风险",
            "状态码",
            "原生/适配",
            "规则",
            "分类",
            "评分",
            "策略",
            "写入",
            "耗时",
            "Trace",
            "原因",
        ],
        f"内容防护探针结果表头必须提供完整排障证据列：{parser.result_headers}",
    )


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
            "外部提供商接口地址必须使用 https://",
            "ipaddress.ip_address",
            "CONTENT_GUARD_MAX_SCAN_BYTES_LIMIT",
            "max_length=512",
            "max_length=4096",
            "max_length=256",
        ]),
        ("app/services/proxy_service.py", [
            "response_payload=client_response",
            "guard_stage = \"stream_buffer\" if stream_guard_buffering else",
            "guard_stage = \"stream_chunk\"",
            "ContentRuntimeGuardService.build_guard_error",
            "content_guard_final_strategy",
            "switch_provider_succeeded",
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
            "?v=20260609-",
            "/content-guard",
            "内容防护",
        ]),
        ("app/templates/content_guard.html", [
            "content-guard-settings-form",
            "content-guard-probe-form",
            "content-guard-external-base-url",
            "浏览器不接收外部 API Key",
            "不含 /responses 或 /chat/completions",
            "content-guard-rules-body",
            "content-guard-page-error",
            "content-guard-rule-search",
            "content-guard-rule-category-filter",
            "content-guard-rule-enabled-filter",
            "content-guard-rule-page-size",
            "content-guard-rule-page-meta",
            "content-guard-inspect-form",
            "content-guard-inspect-endpoint-path",
            "content-guard-inspect-request-payload",
            "content-guard-inspect-url-allowlist",
            "content-guard-result-body",
            "<th>规则</th>",
            "<th>分类</th>",
            "<th>评分</th>",
            "<th>写入</th>",
            "<th>端点</th>",
            "<th>状态码</th>",
            "<th>原生/适配</th>",
            "<th>Trace</th>",
            "content-guard-high-risk-strategy",
            "content-guard-stream-mode",
            "content-guard-max-detection-delay-ms",
            "content-guard-async-review-enabled",
        ]),
        ("app/templates/logs.html", [
            "content-guard-events",
            "内容防护日志",
            "background-jobs",
            "调度任务日志",
        ]),
        ("app/routers/logging_api.py", [
            "TYPED_LOG_EXPORT_FIELDS",
            "def list_content_guard_events",
            "BILLING_RESULT_STATUS_ALIASES",
            "union_all(*event_selects)",
            "_redis_background_job_state_items",
            '"/typed-events/backfill"',
            "matched_rule",
            "result_summary_json",
            "stale_running",
            "fieldnames=TYPED_LOG_EXPORT_FIELDS.get(log_type)",
        ]),
        ("app/logging/adapters/background_job_adapter.py", [
            "RESULT_BY_STATUS",
            "\"running\": \"running\"",
            "\"skipped_lock_unavailable\": \"warning\"",
        ]),
        ("app/tasks.py", [
            "trigger_type = str(kwargs.pop(\"trigger_type\", \"scheduler\")",
            "trigger_type=trigger_type",
            "\"job_run_id\": job_run_id",
            "\"lock_status\": lock_status",
        ]),
        ("app/routers/content_guard.py", [
            'prefix="/api/content-guard"',
            '"/overview"',
            "provider_keyword: str = Query(default=\"\"",
            "provider_page: int = Query(default=1",
            "provider_page_size: int = Query(default=50",
            '"/settings"',
            '"/rules"',
            "@router.get(\"/rules\")",
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
            "list_rules",
            "OVERVIEW_MODEL_LIMIT",
            "RequestLog.log_type.in_(ContentGuardModuleService.ROUTE_TRAFFIC_LOG_TYPES)",
            '"source_index"',
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
            "/api/content-guard/precheck/trust-probe",
            "/api/content-guard/trust-probe",
            "renderContentGuardProbeModalBody",
            "data-action=\"trust-binding\"",
            "content-guard-rules-body",
            "createContentGuardRulesController",
            "loadRulesPage",
            "/api/content-guard/rules?",
            "content-guard-page-error",
            "暂无提供商",
            "content-guard-rule-search",
            "content-guard-rule-category-filter",
            "content-guard-rule-enabled-filter",
            "content-guard-rule-page-size",
            "source_index",
            "showActionToast",
            "confirmDangerAction",
            "规则已从草稿中删除",
            "content-guard-events",
            "matched_rule",
            "Token 回填结果",
            "计费状态",
            "调度任务日志",
            "stale_running",
            "formatContentGuardActionLabel",
            "record_only",
            "async_review",
            "switch_provider",
            "safe_error",
            "content-guard-rule-details",
            "data-rule-field=\"confidence\"",
            "/api/content-guard/runtime/inspect-text",
            "endpoint_path",
            "request_payload",
            "url_allowlist_json",
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
            "/api/content-guard/precheck/trust-probe",
            "浏览器不接收外部 API Key",
            "runGovernanceBatchAction",
            "data-content-guard-provider-evidence",
            "provider_keyword",
            "matched_rules",
            "content_guard_confidence",
            "content_guard_score_delta",
            "content_guard_final_strategy",
        ]),
        ("app/services/provider_service.py", [
            "content_probe_results_json",
            "_parse_content_probe_results",
            "_build_recent_content_guard_events",
            "recent_content_guard_events",
        ]),
        ("app/routers/content_guard.py", [
            "RequestContentGuardEvent",
            "def content_guard_runtime_events",
            "page: int = Query(default=1",
            "page_size: int = Query(default=20",
            "provider_id: int | None",
            "risk_level: str | None",
            "action: str | None",
            "start_at: datetime | None",
            "end_at: datetime | None",
            "select(RequestContentGuardEvent, RequestLog)",
            "select(func.count()).select_from(RequestContentGuardEvent)",
            "def export_content_guard_runtime_events",
        ]),
    ]
    for filename, needles in checks:
        text = Path(filename).read_text(encoding="utf-8", errors="ignore")
        missing = [needle for needle in needles if needle not in text]
        _assert(not missing, f"{filename} 缺少内容治理接线：{missing}")
    runtime_router_text = Path("app/routers/content_guard.py").read_text(encoding="utf-8", errors="ignore")
    runtime_events_body = runtime_router_text.split("def content_guard_runtime_events", 1)[1].split("def export_content_guard_runtime_events", 1)[0]
    _assert("select(RequestContentGuardEvent, RequestLog)" in runtime_events_body, "运行时事件列表必须查询 request_content_guard_events 类型化事件表")
    _assert("select(func.count()).select_from(RequestContentGuardEvent)" in runtime_events_body, "运行时事件分页总数必须基于类型化事件表统计")
    for needle in ("page: int = Query", "page_size: int = Query", "risk_level", "guard_result", "action", "start_at", "end_at"):
        _assert(needle in runtime_events_body, f"运行时事件列表缺少分页或筛选参数：{needle}")
    trust_probe_text = Path("app/services/content_trust_probe_service.py").read_text(encoding="utf-8", errors="ignore")
    _assert("Provider(\n                id=0" not in trust_probe_text, "外部检测禁止继续构造 Provider(id=0)")
    _assert("ProviderModel(\n                id=0" not in trust_probe_text, "外部检测禁止继续构造 ProviderModel(id=0)")
    _assert('"base_url": external.base_url' not in trust_probe_text, "外部检测返回给前端的 target 禁止包含完整 base_url")
    app_css = Path("app/static/css/app.css").read_text(encoding="utf-8", errors="ignore")
    _assert(".content-guard-segment-option:focus-visible" in app_css, "内容防护分段切换必须具备键盘焦点样式")
    _assert(".content-guard-probe-option input:checked + span::before" in app_css, "内容防护探针多选禁止裸露浏览器默认 checkbox")
    _assert('body[data-page="content-guard"] .content-guard-rules-table' in app_css and "table-layout: auto;" in app_css, "内容防护规则表移动端必须取消硬宽并使用卡片化披露")
    content_guard_template = Path("app/templates/content_guard.html").read_text(encoding="utf-8", errors="ignore")
    _assert("用户请求中出现过的域名会自动加入本次白名单" not in content_guard_template, "URL 白名单说明禁止暗示请求体可自动放行域名")
    _assert("content-guard-external-api-key" not in content_guard_template, "内容防护页面禁止保留外部探针 API Key 输入框")
    _assert("content_guard_target_type" not in content_guard_template and "data-content-guard-target-type" in content_guard_template, "内容防护探测目标必须使用分段按钮而不是原生 radio")
    _assert("<th>策略</th>" in content_guard_template and "<th>可信度</th>" not in content_guard_template, "内容防护规则表必须采用核心主列加详情披露")
    _assert('<td colspan="14" class="table-muted">等待检测</td>' in content_guard_template, "探针结果表扩展证据列后空态 colspan 必须同步")
    app_js = Path("app/static/js/app.js").read_text(encoding="utf-8", errors="ignore")
    _assert('patterns: ["待填写"]' not in app_js and 'patterns: [],' in app_js, "新增内容防护规则禁止把占位词作为真实匹配项")
    _assert('window.confirm("恢复默认规则？")' not in app_js, "恢复默认规则禁止继续使用 window.confirm")
    _assert('data-rule-field="reason"' in app_js and 'reason: String(read("reason")?.value || "").trim()' in app_js, "规则编辑必须暴露并保存 reason 字段")
    _assert("必须是有效数字" in app_js and "必须在 ${min} 到 ${max} 之间" in app_js, "内容防护数值字段必须在前端提交前给出明确校验反馈")
    for needle in (
        "data-content-guard-save-rule",
        "data-content-guard-revert-rule",
        "content-guard-rule-dirty-badge",
        "保存本行",
        "已撤销本行修改",
    ):
        _assert(needle in app_js, f"内容防护规则编辑必须支持行级保存、撤销和脏状态：{needle}")
    _assert("existingChecked" in app_js and "selectedProviderId = providerSelect.value" in app_js, "内容防护刷新必须保留探针勾选和提供商选择")
    _assert("content-guard-external-api-key" not in app_js, "内容防护前端禁止继续读取或保留外部探针 API Key")
    for needle in (
        "endpointPath = item.endpoint_path",
        "statusCode = item.status_code",
        "adapted_success",
        "native_success",
        "trace_id || item.trace",
        'colspan="14"',
    ):
        _assert(needle in app_js, f"内容防护探针结果渲染缺少排障字段：{needle}")
    _assert("extract_probe_sse_text_delta" in Path("app/services/content_guard_probe_service.py").read_text(encoding="utf-8", errors="ignore"), "SSE 探针必须聚合流式文本校验固定答案")
    _assert('api.get(`/api/content-guard/overview?${params.toString()}`)' in app_js, "内容防护首页概览必须使用服务端分页/搜索参数")
    _assert(".content-guard-page-error" in app_css and ".content-guard-rules-filter" in app_css and ".toast-action-btn" in app_css, "内容防护错误态、规则筛选和撤销反馈样式缺失")
    _assert(".content-guard-rule-details" in app_css and ".content-guard-rule-main-cell" in app_css, "内容防护规则表详情披露样式缺失")
    benchmark_text = Path("scripts/benchmark_content_guard_overhead.py").read_text(encoding="utf-8", errors="ignore")
    for needle in (
        "runtime_non_stream",
        "stream_prefetch",
        "stream_chunk",
        "log_write_projection",
        "route_retry_projection",
    ):
        _assert(needle in benchmark_text, f"内容防护性能基准必须覆盖 {needle} 成本")
    migration_text = Path("migrations/2026-06-06_add_content_guard_governance.sql").read_text(encoding="utf-8", errors="ignore")
    _assert("trusted_providers_only" not in migration_text, "内容防护正式迁移禁止引入局部只走可信提供商字段")
    main_text = Path("app/main.py").read_text(encoding="utf-8", errors="ignore")
    _assert("CONTENT_GUARD_COMPAT_COLUMNS" in main_text, "启动期兼容 DDL 必须把内容防护字段集中声明，避免迁移口径漂移")
    init_database_body = main_text.split("def init_database", 1)[1].split("def CONTENT_GUARD_COMPAT_COLUMNS", 1)[0]
    _assert(
        "settings.is_production() and not allow_production_ddl" in init_database_body
        and "Base.metadata.create_all(bind=engine)" in init_database_body,
        "生产 Web worker 启动必须在 create_all/兼容 DDL 前由 allow_production_ddl 明确阻断",
    )
    _assert(
        "def _should_run_startup_database_init" in main_text
        and "if settings.is_production():" in main_text
        and "return False" in main_text.split("def _should_run_startup_database_init", 1)[1].split("def _get_table_columns", 1)[0]
        and "if _should_run_startup_database_init():" in main_text
        and "init_database()" in main_text,
        "应用启动期调用 init_database 前必须通过生产环境守卫禁止自动 DDL",
    )
    content_guard_router_text = Path("app/routers/content_guard.py").read_text(encoding="utf-8", errors="ignore")
    router_header = content_guard_router_text.split("DEFAULT_RUNTIME_EVENT_WINDOW_DAYS", 1)[0]
    _assert(
        "dependencies=[Depends(require_admin_api_user)]" in router_header,
        "内容防护 router 必须自带管理员依赖，禁止只依赖 main.py 挂载层保护",
    )


def _check_typed_logging_governance() -> None:
    logging_api_text = Path("app/routers/logging_api.py").read_text(encoding="utf-8", errors="ignore")
    for needle in (
        "TYPED_LOG_TIME_FILTERS",
        "TYPED_LOG_FILTER_ALIASES",
        "_typed_logging_queue_status",
        "_billing_events_summary",
        "BILLING_RESULT_STATUS_ALIASES",
        "_redis_background_job_state_items",
        "_background_job_item_matches",
        "dead_letter",
        "failure_count",
        "backfill_typed_events_from_request_logs",
        "sha256_hex",
        "当前筛选条件没有匹配的类型化日志",
    ):
        _assert(needle in logging_api_text, f"类型化日志接口缺少治理逻辑：{needle}")
    condition = logging_api._filter_condition(type("Column", (), {"in_": lambda self, value: ("in", tuple(value)), "__eq__": lambda self, value: ("eq", value)})(), "result", "成功,失败")
    _assert(condition == ("in", ("success", "filled", "failed")), f"类型化日志过滤必须支持多选与别名：{condition}")
    csv_text = logging_api._items_to_csv([], fieldnames=["id", "created_at"])
    _assert("无数据" in csv_text and "当前筛选条件没有匹配" in csv_text, "类型化日志空导出必须说明无数据原因")
    csv_text = logging_api._items_to_csv(
        [
            {
                "id": 1,
                "trace_id": "trace-stage33",
                "secret_token": "should-not-export",
                "diagnostics_json": {"risk": "low", "nested": ["ok"]},
            }
        ],
        fieldnames=logging_api.TYPED_LOG_EXPORT_FIELDS["content-guard-events"],
    )
    _assert("追踪 ID" in csv_text and "防护结果" in csv_text, "类型化日志 CSV 导出必须使用中文表头")
    _assert("secret_token" not in csv_text and "should-not-export" not in csv_text, "类型化日志 CSV 导出必须按字段白名单输出，禁止泄漏额外敏感字段")
    normalized_csv_text = csv_text.replace('""', '"')
    _assert(
        '"risk": "low"' in normalized_csv_text
        and '"nested": ["ok"]' in normalized_csv_text
        and "['ok']" not in csv_text,
        f"类型化日志 CSV 复杂字段必须使用 JSON 序列化：{csv_text}",
    )

    sink_text = Path("app/logging/sinks.py").read_text(encoding="utf-8", errors="ignore")
    request_adapter_text = Path("app/logging/adapters/request_adapter.py").read_text(encoding="utf-8", errors="ignore")
    log_service_text = Path("app/services/log_service.py").read_text(encoding="utf-8", errors="ignore")
    proxy_text = Path("app/services/proxy_service.py").read_text(encoding="utf-8", errors="ignore")
    _assert("UnregisteredTypedLogEvent" in sink_text and "typed_log_event_unregistered" in sink_text, "未知类型化事件必须写入可观测告警")
    _assert("record_missing_events_from_summary" in request_adapter_text and "_content_guard_event_is_blocked" in request_adapter_text, "请求日志派生事件必须支持幂等回填与防护结果归一")
    _assert('"provider_status_after": content_guard_payload.get("provider_status_after")' in request_adapter_text, "request_logs 派生内容防护事件必须使用 payload 中的 provider_status_after，禁止写成最终策略")
    _assert('"provider_status_after": log.content_guard_final_strategy' not in request_adapter_text, "内容防护 provider_status_after 禁止由 content_guard_final_strategy 合成")
    _assert("backfill_typed_events_from_request_logs" in log_service_text, "历史 request_logs 必须提供类型化事件回填入口")
    _assert("ContentRuntimeGuardService.build_guard_error" in proxy_text and 'final=True' in proxy_text, "内容防护最终错误必须复用统一错误构造")
    _assert("_content_guard_log_kwargs_from_trace" in proxy_text and "switch_provider_exhausted" in proxy_text, "内容防护最终失败日志必须写入结果、风险与最终策略")
    _assert("stream_guard_enabled = ProxyService._content_guard_enabled_for_request" in proxy_text and "adapt_chat_response_to_responses" not in proxy_text.split("stream_guard_enabled = ProxyService._content_guard_enabled_for_request", 1)[1].split("stream_guard_mode", 1)[0], "协议适配场景禁止关闭流式内容防护")

    queue_text = Path("app/logging/queue.py").read_text(encoding="utf-8", errors="ignore")
    config_text = Path("app/config.py").read_text(encoding="utf-8", errors="ignore")
    env_text = Path(".env.example").read_text(encoding="utf-8", errors="ignore")
    _assert("logging_event_queue_require_local_worker" in config_text and "LOGGING_EVENT_QUEUE_REQUIRE_LOCAL_WORKER=false" in env_text, "类型化日志队列必须支持独立 worker 架构")
    _assert("DEAD_LETTER_KEY" in queue_text and "FAILURE_COUNT_KEY" in queue_text and "_record_worker_failure" in queue_text, "类型化日志队列批次失败必须有死信和失败计数")
    _assert("asyncio.wait(workers, timeout=2)" in queue_text and "worker.cancel()" in queue_text, "类型化日志队列停机必须先尝试 drain 再取消")
    enqueue_body = queue_text.split("def enqueue", 1)[1].split("def _has_active_workers", 1)[0]
    _assert("not cls._has_active_workers()" not in enqueue_body or "logging_event_queue_require_local_worker" in enqueue_body, "类型化日志入队禁止强依赖当前 Web 进程本地 worker")

    asset_model = Path("app/models/logging_events.py").read_text(encoding="utf-8", errors="ignore")
    asset_adapter = Path("app/logging/adapters/asset_adapter.py").read_text(encoding="utf-8", errors="ignore")
    migration = Path("migrations/2026-06-09_extend_typed_logging_details.sql").read_text(encoding="utf-8", errors="ignore")
    _assert("sha256_hex" in asset_model and '"sha256_hex": sha256_hex' in asset_adapter and "ADD COLUMN IF NOT EXISTS sha256_hex" in migration, "素材日志必须记录完整 SHA256 并提供迁移")
    for table_name in (
        "exception_events",
        "health_check_runs",
        "token_finalize_events",
        "billing_process_events",
        "background_job_events",
        "asset_events",
    ):
        _assert(f"CREATE TABLE IF NOT EXISTS {table_name}" in migration, f"类型化日志正式迁移缺少表：{table_name}")
    _assert(
        "backfill_typed_events_from_request_logs" in log_service_text and '"/typed-events/backfill"' in logging_api_text,
        "历史 request_logs 必须提供类型化日志回填服务和接口",
    )

    exception_adapter = Path("app/logging/adapters/exception_adapter.py").read_text(encoding="utf-8", errors="ignore")
    main_text = Path("app/main.py").read_text(encoding="utf-8", errors="ignore")
    _assert("max_string_length=1200" in exception_adapter and "max_bytes=8192" in exception_adapter, "异常日志详情必须脱敏并收敛展示长度")
    http_handler_body = main_text.split("async def http_exception_handler", 1)[1].split("@app.exception_handler(Exception)", 1)[0]
    _assert("if exc.status_code >= 500:" in http_handler_body, "普通 HTTP 4xx 禁止进入异常事件表")

    logs_template = Path("app/templates/logs.html").read_text(encoding="utf-8", errors="ignore")
    app_js = Path("app/static/js/app.js").read_text(encoding="utf-8", errors="ignore")
    app_css = Path("app/static/css/app.css").read_text(encoding="utf-8", errors="ignore")
    for needle in (
        'id="logs-page-title"',
        'role="tablist"',
        'role="tab"',
        'aria-selected="true"',
        'aria-selected="false"',
        'id="logs-request-panel"',
        'id="logs-typed-panel"',
    ):
        _assert(needle in logs_template, f"日志页 Tab 语义和动态标题结构缺失：{needle}")
    for needle in (
        "typed-logs-summary",
        "typed-logs-meta-strip",
    ):
        _assert(needle in logs_template, f"日志页缺少类型化日志摘要容器：{needle}")
    for needle in (
        "renderTypedSummary",
        "renderTypedMetaStrip",
        "applyTypedTimeFilterMeta",
        "Object.entries(item || {})",
        "完整 SHA256",
        "配置保留期",
        "时间筛选字段",
        "失败 / 待处理",
        "requestLogsAvailable",
        "typedPaginationState",
        "snapshotCurrentTypedPagination",
        "restoreTypedPagination",
        "resetCurrentTypedPagination",
        "typed_page",
        "typed_page_size",
        "typed_log_type",
        "logsPageTitle.textContent",
        'button.setAttribute("aria-selected"',
    ):
        _assert(needle in app_js, f"日志页前端缺少类型化日志治理展示：{needle}")
    _assert("typedState.page = 1;\n            renderTypedFilterControls(normalizedTab)" not in app_js, "类型日志 Tab 切换禁止强制丢失分页")
    _assert("params.set(\"log_type\", state.activeTab)" not in app_js, "类型日志导出禁止继续复用 log_type 作为分支参数")
    _assert("typed_log_type: str | None" in logging_api_text and "resolved_log_type = typed_log_type or log_type" in logging_api_text, "类型日志导出必须支持 typed_log_type 并兼容旧 log_type")
    _assert(".typed-logs-summary-grid" in app_css and ".typed-logs-meta-strip" in app_css, "类型化日志摘要样式缺失")


def main() -> None:
    _check_content_guard_detection()
    _check_content_guard_rule_service_boundaries()
    _check_response_structure_detection()
    _check_router_content_policy()
    _check_runtime_guard_request_semantics()
    _check_content_guard_record_violation_semantics()
    _check_low_trust_route_field_hidden()
    _check_high_risk_route_detection()
    _check_content_guard_route_cache_and_scheduler_policy()
    _check_content_guard_retry_probe_semantics()
    asyncio.run(_check_proxy_db_write_transaction_boundary())
    asyncio.run(_check_stream_guard_pending_prefetch_before_downstream())
    _check_content_guard_frontend_policy_wiring()
    _check_content_guard_schema_and_audit_contracts()
    _check_error_catalog()
    _check_health_probe_guard_helpers()
    asyncio.run(_check_scheduled_content_probe_batch_policy())
    _check_content_probe_health_window_and_recovery()
    _check_trust_probe_keys_and_probe_boundaries()
    _check_manual_trust_edit_trace()
    _check_content_guard_metrics_alerts()
    _check_content_guard_auto_isolation()
    _check_monitoring_alert_write_stability()
    _check_collect_applies_monitoring_actions_without_refresh()
    asyncio.run(_check_distributed_lock_skips_when_redis_unavailable())
    _check_content_guard_disabled_mode()
    _check_external_probe_security_boundaries()
    _check_content_probe_target_boundaries()
    _check_content_guard_settings_validation()
    _check_settings_submit_affects_runtime()
    _check_content_guard_migrations_build_typed_tables()
    _check_content_guard_browser_smoke()
    _check_content_guard_api_route_smoke()
    _check_content_guard_template_dom_contract()
    _check_frontend_and_log_wiring()
    _check_typed_logging_governance()
    print("stage33 content guard regression check passed")


if __name__ == "__main__":
    main()
