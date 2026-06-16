from __future__ import annotations

import json
import re
import html
import ipaddress
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.alert_event import AlertEvent
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.utils.json_utils import dumps_json


@dataclass(slots=True)
class ContentGuardResult:
    result: str
    risk_level: str
    categories: list[str] = field(default_factory=list)
    reason: str = ""
    action: str = "allow"
    excerpt: str | None = None
    score_delta: int = 0
    confidence: float = 0.0
    matched_rules: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int | None = None
    buffer_wait_ms: int | None = None
    final_strategy: str | None = None

    def to_log_kwargs(self) -> dict[str, Any]:
        return {
            "content_guard_result": self.result,
            "content_guard_risk_level": self.risk_level,
            "content_guard_categories_json": dumps_json(self.categories),
            "content_guard_reason": self.reason,
            "content_guard_action": self.action,
            "content_guard_excerpt": self.excerpt,
            "content_guard_latency_ms": self.latency_ms,
            "content_guard_buffer_wait_ms": self.buffer_wait_ms,
            "content_guard_final_strategy": self.final_strategy,
            "content_guard_confidence": self.confidence,
            "content_guard_score_delta": self.score_delta,
        }

    def matched_rules_json(self) -> str:
        return dumps_json(self.matched_rules or None)


@dataclass(slots=True)
class ContentGuardRule:
    id: str
    name: str
    category: str
    enabled: bool = True
    match_type: str = "keyword_any"
    patterns: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    action: str = "record"
    score_delta: int = -8
    confidence: float = 0.7
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "enabled": self.enabled,
            "match_type": self.match_type,
            "patterns": list(self.patterns),
            "risk_level": self.risk_level,
            "action": self.action,
            "score_delta": self.score_delta,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class ContentGuardService:
    """独立内容完整性防护服务，负责上游输出污染检测与处置结果生成。"""

    RESULT_PASS = "pass"
    RESULT_REVIEW = "review"
    RESULT_BLOCK = "block"
    RESULT_ERROR = "error"
    TRUSTED_LEVELS = {"official", "trusted"}
    STRUCTURED_ENDPOINTS = {"/chat/completions", "/responses", "/completions"}
    EXCERPT_MAX_CHARS = 500
    MAX_SCAN_NODES = 2000
    MAX_SCAN_DEPTH = 32
    JSON_MODE_TYPES = {"json_object", "json_schema"}
    SUSPICIOUS_RESPONSE_KEYS = {
        "ad",
        "ads",
        "advertisement",
        "affiliate",
        "contact",
        "promo",
        "promotion",
        "qr",
        "qr_code",
        "qrcode",
        "sponsor",
        "telegram",
        "wechat",
        "whatsapp",
    }
    SUSPICIOUS_KEY_SCAN_SKIP_KEYS = {
        "metadata",
        "usage",
        "system_fingerprint",
        "service_tier",
        "billing",
        "logprobs",
        "content",
        "input",
        "messages",
        "text",
        "output_text",
        "json",
        "arguments",
        "summary",
        "reasoning",
        "annotations",
        "citations",
        "refusal",
        "image_url",
    }

    _URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
    _URL_MARKERS = ("http://", "https://")
    _ALLOWED_RULE_MATCH_TYPES = {"keyword_any", "regex", "unexpected_url"}
    _ALLOWED_RULE_RISK_LEVELS = {"low", "medium", "high"}
    _ALLOWED_RULE_ACTIONS = {"allow", "record", "block"}
    _ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
    _DOMAIN_RE = re.compile(
        r"(?<![\w.-])(?:[a-z0-9\u4e00-\u9fff](?:[a-z0-9\u4e00-\u9fff-]{0,61}[a-z0-9\u4e00-\u9fff])?\.)+"
        r"(?:[a-z\u4e00-\u9fff]{2,63}|xn--[a-z0-9-]{2,59})(?![\w.-])",
        re.IGNORECASE,
    )
    _IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
    _IPV6_RE = re.compile(r"(?<![\w:])(?:\[[0-9a-f:.%]+\]|[0-9a-f]{0,4}:[0-9a-f:.%]{2,})(?![\w:])", re.IGNORECASE)
    SHORT_LINK_DOMAINS = {
        "amzn.to",
        "bit.ly",
        "bitly.com",
        "tinyurl.com",
        "t.co",
        "goo.gl",
        "ow.ly",
        "is.gd",
        "buff.ly",
        "cutt.ly",
        "rebrand.ly",
        "s.id",
        "t.cn",
        "dwz.cn",
        "url.cn",
        "sourl.cn",
        "lnkd.in",
        "trib.al",
        "ift.tt",
        "soo.gd",
        "shorturl.at",
        "rb.gy",
        "v.gd",
        "clck.ru",
        "cutt.us",
        "u.to",
    }
    AD_INTENT_TERMS = (
        "优惠",
        "折扣",
        "返利",
        "返佣",
        "邀请码",
        "推广",
        "开户链接",
        "注册",
        "充值",
        "扫码",
        "加群",
        "客服",
        "代理",
        "sponsor",
        "promo",
        "coupon",
        "affiliate",
        "referral",
        "join our",
        "contact us",
    )
    DEFAULT_TEXT_RULES: tuple[ContentGuardRule, ...] = (
        ContentGuardRule(
            id="fraud_or_illegal_promotion",
            name="违规推广",
            category="fraud_or_illegal_promotion",
            match_type="regex",
            patterns=[
                r"(博彩|赌博|网赌|百家乐|盘口|杀猪盘|裸聊|色情|代充|套现|洗钱|诈骗).{0,24}(群|链接|开户链接|开户|注册|充值|返利|返佣|代理|客服|扫码|扫描|联系|访问|加入)",
                r"(扫码|扫描|加入|访问|联系|私聊|开户|注册|充值).{0,24}(博彩|赌博|网赌|百家乐|盘口|杀猪盘|裸聊|色情|代充|套现|洗钱|诈骗)",
            ],
            risk_level="high",
            action="block",
            score_delta=-25,
            confidence=0.92,
            reason="命中博彩、诈骗、洗钱或色情等违规导流组合",
        ),
        ContentGuardRule(
            id="qr_code_redirect",
            name="二维码导流",
            category="qr_code_redirect",
            match_type="regex",
            patterns=[
                r"(二维码|扫码|扫二维码|扫描下方).{0,24}(加群|进群|联系|添加|领取|优惠|返利|充值|注册|客服|代理|群)",
            ],
            risk_level="high",
            action="block",
            score_delta=-25,
            confidence=0.9,
            reason="命中二维码或扫码导流组合",
        ),
        ContentGuardRule(
            id="advertising_or_promotion",
            name="广告推广",
            category="advertising_or_promotion",
            match_type="keyword_any",
            patterns=["优惠码", "邀请码", "注册送", "充值返现", "推广链接", "赞助商", "折扣码", "返佣", "开户链接"],
            risk_level="medium",
            action="record",
            score_delta=-8,
            confidence=0.76,
            reason="命中广告、赞助、优惠或推广短语",
        ),
        ContentGuardRule(
            id="contact_or_offplatform_redirect",
            name="站外联系",
            category="contact_or_offplatform_redirect",
            match_type="regex",
            patterns=[
                r"(添加|联系|私聊|加|加入|进).{0,10}(微信|wechat|qq|telegram|whatsapp|群|社群|社区|客服)",
                r"(添加|联系|私聊|加|加入|进).{0,12}(微\s*信|w\s*e\s*c\s*h\s*a\s*t|q\s*q|telegram|whatsapp|群|社群|社区|客服)",
                r"(微信|wechat|qq|telegram|whatsapp).{0,8}(号|群|联系|私聊|客服|添加)",
                r"(微\s*信|w\s*e\s*c\s*h\s*a\s*t|q\s*q|telegram|whatsapp).{0,10}(号|群|联系|私聊|客服|添加|[a-z0-9_-]{3,})",
                r"(加群|进群|联系我|私聊|加入社群|加入社区)",
            ],
            risk_level="medium",
            action="record",
            score_delta=-8,
            confidence=0.78,
            reason="命中站外联系方式或社群引流组合",
        ),
        ContentGuardRule(
            id="api_key_community_promotion",
            name="Key 社群引流",
            category="advertising_or_promotion",
            match_type="keyword_any",
            patterns=["公益key", "免费key", "共享key", "key开放", "key will be changed", "join the new community"],
            risk_level="high",
            action="block",
            score_delta=-25,
            confidence=0.88,
            reason="命中 API Key 或模型社群引流内容",
        ),
        ContentGuardRule(
            id="unexpected_link",
            name="异常外链",
            category="unexpected_link",
            match_type="unexpected_url",
            patterns=["http://", "https://"],
            risk_level="medium",
            action="record",
            score_delta=-8,
            confidence=0.68,
            reason="请求未要求广告或链接时，响应中出现外部链接",
        ),
    )

    @classmethod
    def requires_trusted_provider(
        cls,
        *,
        payload: dict[str, Any] | None,
        endpoint_path: str | None = None,
        has_image: bool = False,
        require_tools: bool = False,
    ) -> bool:
        """判断请求是否应优先走可信提供商，不执行可用探测。"""
        try:
            if not bool(getattr(SettingService.get_cached(), "content_guard_enabled", True)):
                return False
        except Exception:
            pass
        if not isinstance(payload, dict):
            return bool(has_image or require_tools)
        return bool(
            has_image
            or require_tools
            or cls._request_expects_json_response(payload)
            or cls._payload_contains_file_reference(payload)
            or cls._payload_has_long_context(payload)
        )

    @classmethod
    def inspect_response_text(
        cls,
        text: str | None,
        *,
        provider: Provider | None = None,
        endpoint_path: str | None = None,
        request_payload: dict[str, Any] | None = None,
        max_scan_bytes: int = 16384,
        rules_json: str | None = None,
        url_allowlist: list[str] | str | None = None,
        url_check_enabled: bool = True,
        rules: list[ContentGuardRule | dict[str, Any]] | None = None,
        enhanced_detection_enabled: bool | None = None,
        enhanced_illegal_enabled: bool | None = None,
        enhanced_ad_enabled: bool | None = None,
        enhanced_custom_enabled: bool | None = None,
        enhanced_obfuscation_enabled: bool | None = None,
        enhanced_threshold: int | None = None,
        enhanced_context_window_chars: int | None = None,
    ) -> ContentGuardResult:
        from app.services.content_guard_rule_service import ContentGuardRuleService

        started = now_beijing()
        sample = cls._clip_text(cls.normalize_scan_text(text), max_scan_bytes=max_scan_bytes)
        if not sample:
            return ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="未检测到可扫描文本")
        enhanced_options = cls._resolve_enhanced_detection_options(
            enhanced_detection_enabled=enhanced_detection_enabled,
            enhanced_illegal_enabled=enhanced_illegal_enabled,
            enhanced_ad_enabled=enhanced_ad_enabled,
            enhanced_custom_enabled=enhanced_custom_enabled,
            enhanced_obfuscation_enabled=enhanced_obfuscation_enabled,
            enhanced_threshold=enhanced_threshold,
            enhanced_context_window_chars=enhanced_context_window_chars,
        )
        matched_rules = cls.match_text_rules(
            sample,
            request_payload=request_payload,
            url_allowlist=url_allowlist,
            url_check_enabled=url_check_enabled,
            rules=rules if rules is not None else ContentGuardRuleService.parse_rules_json(rules_json),
            **enhanced_options,
        )
        active_rules = ContentGuardRuleService.normalize_rules(
            rules if rules is not None else ContentGuardRuleService.parse_rules_json(rules_json)
        )
        invalid_config_rules = [rule for rule in active_rules if rule.category == "rule_configuration_error"]
        if invalid_config_rules:
            matched_rules.extend(rule for rule in invalid_config_rules if rule.id not in {item.id for item in matched_rules})
        invalid_regex_rules = [rule for rule in matched_rules if rule.category == "rule_configuration_error"]
        if invalid_regex_rules:
            return cls._with_latency(
                ContentGuardResult(
                    result=cls.RESULT_ERROR,
                    risk_level="high",
                    categories=["rule_configuration_error"],
                    reason=invalid_regex_rules[0].reason or "内容防护正则规则配置无效",
                    action="record",
                    excerpt=invalid_regex_rules[0].patterns[0] if invalid_regex_rules[0].patterns else None,
                    score_delta=0,
                    confidence=1.0,
                    matched_rules=[rule.to_dict() for rule in invalid_regex_rules],
                ),
                started=started,
            )
        if not matched_rules:
            return cls._with_latency(
                ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="未命中内容污染规则"),
                started=started,
            )
        allow_rules, risk_rules = ContentGuardRuleService.split_allow_rules(matched_rules)
        if allow_rules:
            return cls._with_latency(
                ContentGuardResult(
                    result=cls.RESULT_PASS,
                    risk_level="low",
                    categories=[rule.category for rule in allow_rules if rule.category],
                    reason=cls._summarize_matched_rules(allow_rules) or "命中内容防护放行规则",
                    action="allow",
                    excerpt=cls._excerpt(sample),
                    score_delta=0,
                    confidence=cls._combine_rule_confidence(allow_rules),
                    matched_rules=[rule.to_dict() for rule in allow_rules],
                ),
                started=started,
            )
        matched_rules = risk_rules
        categories = []
        for rule in matched_rules:
            if rule.category not in categories:
                categories.append(rule.category)
        high_risk = any(rule.risk_level == "high" or rule.action == "block" for rule in matched_rules)
        score_delta, score_total = ContentGuardRuleService.score_rules(matched_rules)
        if score_total > 0:
            score_delta = -min(100, max(8, score_total))
        confidence = cls._combine_rule_confidence(matched_rules)
        tail_boost = cls._tail_risk_boost(sample, matched_rules)
        ad_intent_boost = 0.08 if cls._has_ad_intent(sample) else 0.0
        confidence = max(0.0, min(1.0, confidence + tail_boost + ad_intent_boost))
        reason = cls._summarize_matched_rules(matched_rules)
        if high_risk or score_total >= 25 or (len(categories) >= 2 and not cls._request_allows_advertising(request_payload)):
            return cls._with_latency(
                ContentGuardResult(
                    result=cls.RESULT_BLOCK,
                    risk_level="high",
                    categories=categories,
                    reason=reason or "上游输出疑似追加推广、联系方式、二维码或违规引流内容",
                    action="block",
                    excerpt=cls._excerpt(sample),
                    score_delta=score_delta,
                    confidence=confidence,
                    matched_rules=[rule.to_dict() for rule in matched_rules],
                ),
                started=started,
            )
        return cls._with_latency(
            ContentGuardResult(
                result=cls.RESULT_REVIEW,
                risk_level="medium",
                categories=categories,
                reason=reason or "上游输出命中可疑推广或外链规则，已记录复核",
                action="record",
                excerpt=cls._excerpt(sample),
                score_delta=score_delta,
                confidence=confidence,
                matched_rules=[rule.to_dict() for rule in matched_rules],
            ),
            started=started,
        )

    @classmethod
    def match_text_rules(
        cls,
        text: str,
        *,
        request_payload: dict[str, Any] | None = None,
        url_allowlist: list[str] | str | None = None,
        url_check_enabled: bool = True,
        rules: list[ContentGuardRule | dict[str, Any]] | None = None,
        enhanced_detection_enabled: bool = True,
        enhanced_illegal_enabled: bool = True,
        enhanced_ad_enabled: bool = True,
        enhanced_custom_enabled: bool = True,
        enhanced_obfuscation_enabled: bool = True,
        enhanced_threshold: int = 70,
        enhanced_context_window_chars: int = 96,
    ) -> list[ContentGuardRule]:
        from app.services.content_guard_rule_service import ContentGuardRuleService

        return ContentGuardRuleService.match_text_rules(
            text,
            request_payload=request_payload,
            url_allowlist=url_allowlist,
            url_check_enabled=url_check_enabled,
            rules=rules,
            enhanced_detection_enabled=enhanced_detection_enabled,
            enhanced_illegal_enabled=enhanced_illegal_enabled,
            enhanced_ad_enabled=enhanced_ad_enabled,
            enhanced_custom_enabled=enhanced_custom_enabled,
            enhanced_obfuscation_enabled=enhanced_obfuscation_enabled,
            enhanced_threshold=enhanced_threshold,
            enhanced_context_window_chars=enhanced_context_window_chars,
        )

    @classmethod
    def inspect_json_response(
        cls,
        payload: Any,
        *,
        provider: Provider | None = None,
        provider_model: ProviderModel | None = None,
        endpoint_path: str | None = None,
        request_payload: dict[str, Any] | None = None,
        max_scan_bytes: int = 16384,
        rules_json: str | None = None,
        url_allowlist: list[str] | str | None = None,
        url_check_enabled: bool = True,
        rules: list[ContentGuardRule | dict[str, Any]] | None = None,
        enhanced_detection_enabled: bool | None = None,
        enhanced_illegal_enabled: bool | None = None,
        enhanced_ad_enabled: bool | None = None,
        enhanced_custom_enabled: bool | None = None,
        enhanced_obfuscation_enabled: bool | None = None,
        enhanced_threshold: int | None = None,
        enhanced_context_window_chars: int | None = None,
    ) -> ContentGuardResult:
        started = now_beijing()
        enhanced_options = cls._resolve_enhanced_detection_options(
            enhanced_detection_enabled=enhanced_detection_enabled,
            enhanced_illegal_enabled=enhanced_illegal_enabled,
            enhanced_ad_enabled=enhanced_ad_enabled,
            enhanced_custom_enabled=enhanced_custom_enabled,
            enhanced_obfuscation_enabled=enhanced_obfuscation_enabled,
            enhanced_threshold=enhanced_threshold,
            enhanced_context_window_chars=enhanced_context_window_chars,
        )
        structure_result = cls._inspect_response_structure(
            payload,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            max_scan_bytes=max_scan_bytes,
            rules_json=rules_json,
            url_allowlist=url_allowlist,
            url_check_enabled=url_check_enabled,
            rules=rules,
            **enhanced_options,
        )
        if structure_result.result == cls.RESULT_BLOCK:
            return cls._with_latency(structure_result, started=started)
        text = cls._extract_response_scan_text(payload, endpoint_path=endpoint_path, max_scan_bytes=max_scan_bytes)
        text_result = cls.inspect_response_text(
            text,
            provider=provider,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            max_scan_bytes=max_scan_bytes,
            rules_json=rules_json,
            url_allowlist=url_allowlist,
            url_check_enabled=url_check_enabled,
            rules=rules,
            **enhanced_options,
        )
        if text_result.result != cls.RESULT_PASS:
            return text_result
        return cls._with_latency(structure_result, started=started)

    @classmethod
    def inspect_sse_event(
        cls,
        data: str,
        *,
        endpoint_path: str | None = None,
        request_payload: dict[str, Any] | None = None,
        max_scan_bytes: int = 16384,
        rules_json: str | None = None,
        url_allowlist: list[str] | str | None = None,
        url_check_enabled: bool = True,
        rules: list[ContentGuardRule | dict[str, Any]] | None = None,
        enhanced_detection_enabled: bool | None = None,
        enhanced_illegal_enabled: bool | None = None,
        enhanced_ad_enabled: bool | None = None,
        enhanced_custom_enabled: bool | None = None,
        enhanced_obfuscation_enabled: bool | None = None,
        enhanced_threshold: int | None = None,
        enhanced_context_window_chars: int | None = None,
    ) -> ContentGuardResult:
        started = now_beijing()
        if not data or data == "[DONE]":
            return cls._with_latency(
                ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="SSE 控制事件"),
                started=started,
            )
        stripped = data.strip()
        if not stripped.startswith("{"):
            return cls._with_latency(
                ContentGuardResult(
                    result=cls.RESULT_REVIEW,
                    risk_level="medium",
                    categories=["invalid_sse_event"],
                    reason="SSE data 不是 JSON 事件，已按兼容流文本降级复核",
                    action="async_review",
                    excerpt=cls._excerpt(stripped),
                    score_delta=-5,
                ),
                started=started,
            )
        try:
            payload = json.loads(stripped)
        except Exception:
            return cls._with_latency(
                ContentGuardResult(
                    result=cls.RESULT_REVIEW,
                    risk_level="medium",
                    categories=["invalid_sse_event"],
                    reason="SSE data 不是合法 JSON 事件，已按兼容流文本降级复核",
                    action="async_review",
                    excerpt=cls._excerpt(stripped),
                    score_delta=-5,
                ),
                started=started,
            )
        text = cls._extract_response_scan_text(payload, endpoint_path=endpoint_path, max_scan_bytes=max_scan_bytes)
        if not text:
            return cls._with_latency(
                ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="SSE 事件未发现可扫描输出文本"),
                started=started,
            )
        return cls.inspect_response_text(
            text,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            max_scan_bytes=max_scan_bytes,
            rules_json=rules_json,
            url_allowlist=url_allowlist,
            url_check_enabled=url_check_enabled,
            rules=rules,
            enhanced_detection_enabled=enhanced_detection_enabled,
            enhanced_illegal_enabled=enhanced_illegal_enabled,
            enhanced_ad_enabled=enhanced_ad_enabled,
            enhanced_custom_enabled=enhanced_custom_enabled,
            enhanced_obfuscation_enabled=enhanced_obfuscation_enabled,
            enhanced_threshold=enhanced_threshold,
            enhanced_context_window_chars=enhanced_context_window_chars,
        )

    @classmethod
    def default_rules(cls) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in cls.DEFAULT_TEXT_RULES]

    @classmethod
    def parse_rules_json(cls, rules_json: str | None) -> list[ContentGuardRule]:
        from app.services.content_guard_rule_service import ContentGuardRuleService

        return ContentGuardRuleService.parse_rules_json(rules_json)

    @classmethod
    def _resolve_enhanced_detection_options(
        cls,
        *,
        enhanced_detection_enabled: bool | None = None,
        enhanced_illegal_enabled: bool | None = None,
        enhanced_ad_enabled: bool | None = None,
        enhanced_custom_enabled: bool | None = None,
        enhanced_obfuscation_enabled: bool | None = None,
        enhanced_threshold: int | None = None,
        enhanced_context_window_chars: int | None = None,
    ) -> dict[str, Any]:
        setting = None
        try:
            setting = SettingService.get_cached()
        except Exception:
            setting = None

        def bool_value(explicit: bool | None, field_name: str, default: bool) -> bool:
            if explicit is not None:
                return bool(explicit)
            if setting is None:
                return default
            return bool(getattr(setting, field_name, default))

        def int_value(explicit: int | None, field_name: str, default: int, minimum: int, maximum: int) -> int:
            if explicit is not None:
                raw = explicit
            elif setting is not None:
                raw = getattr(setting, field_name, default)
            else:
                raw = default
            try:
                value = int(raw)
            except Exception:
                value = default
            return min(maximum, max(minimum, value))

        return {
            "enhanced_detection_enabled": bool_value(enhanced_detection_enabled, "content_guard_enhanced_detection_enabled", True),
            "enhanced_illegal_enabled": bool_value(enhanced_illegal_enabled, "content_guard_enhanced_illegal_enabled", True),
            "enhanced_ad_enabled": bool_value(enhanced_ad_enabled, "content_guard_enhanced_ad_enabled", True),
            "enhanced_custom_enabled": bool_value(enhanced_custom_enabled, "content_guard_enhanced_custom_enabled", True),
            "enhanced_obfuscation_enabled": bool_value(enhanced_obfuscation_enabled, "content_guard_enhanced_obfuscation_enabled", True),
            "enhanced_threshold": int_value(enhanced_threshold, "content_guard_enhanced_threshold", 70, 0, 100),
            "enhanced_context_window_chars": int_value(
                enhanced_context_window_chars,
                "content_guard_enhanced_context_window_chars",
                96,
                24,
                512,
            ),
        }

    @classmethod
    def _invalid_rules_json_rule(cls, reason: str) -> ContentGuardRule:
        return ContentGuardRule(
            id="invalid_rules_json",
            name="规则配置损坏",
            category="rule_configuration_error",
            match_type="keyword_any",
            patterns=["__invalid_content_guard_rules_json__"],
            risk_level="high",
            action="record",
            score_delta=0,
            confidence=1.0,
            reason=reason[:200],
        )

    @classmethod
    def serialize_rules_json(cls, rules: list[ContentGuardRule | dict[str, Any]] | None) -> str:
        from app.services.content_guard_rule_service import ContentGuardRuleService

        return ContentGuardRuleService.serialize_rules_json(rules)

    @classmethod
    def normalize_rules(cls, rules: list[ContentGuardRule | dict[str, Any]] | None) -> list[ContentGuardRule]:
        from app.services.content_guard_rule_service import ContentGuardRuleService

        return ContentGuardRuleService.normalize_rules(rules)

    @classmethod
    def _coerce_rule(cls, item: Any, *, index: int) -> ContentGuardRule | None:
        from app.services.content_guard_rule_service import ContentGuardRuleService

        return ContentGuardRuleService.coerce_rule(item, index=index)

    @staticmethod
    def _normalize_rule_id(value: str) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")[:64]

    @staticmethod
    def _summarize_matched_rules(rules: list[ContentGuardRule]) -> str:
        summaries: list[str] = []
        for rule in rules[:3]:
            name = rule.name or rule.id
            if not name:
                continue
            patterns = [str(item).strip() for item in rule.patterns if str(item).strip()]
            pattern_hint = ""
            if patterns:
                clipped = ", ".join(pattern[:40] for pattern in patterns[:2])
                pattern_hint = f"，pattern={clipped}"
            summaries.append(
                f"{name}(id={rule.id}, type={rule.match_type}, confidence={float(rule.confidence or 0):.2f}{pattern_hint})"
            )
        if not summaries:
            return ""
        return "命中内容防护规则：" + "；".join(summaries)

    @staticmethod
    def _with_latency(result: ContentGuardResult, *, started: datetime) -> ContentGuardResult:
        result.latency_ms = max(0, int((now_beijing() - started).total_seconds() * 1000))
        return result

    @classmethod
    def normalize_scan_text(cls, text: str | None) -> str:
        if not isinstance(text, str) or not text:
            return ""
        normalized = unicodedata.normalize("NFKC", text)
        normalized = normalized.translate(str.maketrans({"．": ".", "｡": "."}))
        normalized = cls._ZERO_WIDTH_RE.sub("", normalized)
        normalized = html.unescape(normalized)
        previous = normalized
        for _ in range(2):
            decoded = unquote(previous)
            if decoded == previous:
                break
            previous = decoded
        return cls._normalize_obfuscated_url_text(previous)

    @classmethod
    def parse_url_allowlist(cls, value: list[str] | str | None) -> set[str]:
        domains: set[str] = set()
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return domains
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    value = parsed
                else:
                    value = []
            except Exception:
                if stripped.startswith("{"):
                    value = []
                else:
                    value = [item.strip() for item in stripped.replace(",", "\n").splitlines()]
        if isinstance(value, list):
            for item in value:
                raw_item = str(item).strip()
                wildcard = raw_item.startswith("*.")
                domain = cls._normalize_domain(raw_item[2:] if wildcard else raw_item)
                if domain:
                    domains.add(f"*.{domain}" if wildcard else domain)
        return domains

    @classmethod
    def allowed_domains(
        cls,
        *,
        request_payload: dict[str, Any] | None,
        url_allowlist: list[str] | str | None = None,
    ) -> set[str]:
        return cls.parse_url_allowlist(url_allowlist)

    @classmethod
    def extract_domains(cls, text: str | None) -> set[str]:
        normalized = cls.normalize_scan_text(text)
        domains: set[str] = set()
        for url in cls._URL_RE.findall(normalized):
            parsed = urlparse(url)
            domain = cls._normalize_domain(parsed.hostname or "")
            if domain:
                domains.add(domain)
        for match in cls._DOMAIN_RE.findall(normalized):
            domain = cls._normalize_domain(match)
            if domain:
                domains.add(domain)
        for match in cls._IPV4_RE.findall(normalized):
            domain = cls._normalize_domain(match)
            if domain:
                domains.add(domain)
        for match in cls._IPV6_RE.findall(normalized):
            domain = cls._normalize_domain(match)
            if domain:
                domains.add(domain)
        return domains

    @staticmethod
    def _normalize_domain(value: str) -> str:
        domain = value.strip().lower()
        domain = domain.strip("[](){}<>\"'`，,；;。")
        domain = domain.rstrip(".")
        if not domain:
            return ""
        if "://" in domain:
            domain = urlparse(domain).hostname or ""
        if domain.startswith("[") and "]" in domain:
            domain = domain[1:domain.index("]")]
        elif ":" in domain and domain.count(":") == 1:
            host, port = domain.rsplit(":", 1)
            if port.isdigit():
                domain = host
        domain = domain.strip().lower().rstrip(".")
        if "%" in domain:
            domain = domain.split("%", 1)[0]
        if not domain:
            return ""
        try:
            return ipaddress.ip_address(domain).compressed.lower()
        except ValueError:
            pass
        domain = domain.removeprefix("www.")
        if not domain or "." not in domain:
            return ""
        try:
            ascii_domain = domain.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return ""
        if len(ascii_domain) > 253:
            return ""
        labels = ascii_domain.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        ):
            return ""
        return ascii_domain

    @staticmethod
    def _normalize_obfuscated_url_text(value: str) -> str:
        normalized = re.sub(r"\bhxxps://", "https://", value, flags=re.IGNORECASE)
        normalized = re.sub(r"\bhxxp://", "http://", normalized, flags=re.IGNORECASE)
        normalized = re.sub(
            r"(?<=[a-z0-9])\s*(?:\[\.\]|\(\.\)|\{\.\}|\[dot\]|\(dot\)|\{dot\}|dot|点)\s*(?=[a-z0-9])",
            ".",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(
            r"\bwww\s*\.\s*([a-z0-9][a-z0-9.-]+\.[a-z]{2,63})\b",
            r"www.\1",
            normalized,
            flags=re.IGNORECASE,
        )
        return normalized

    @staticmethod
    def _domain_allowed(domain: str, allowed_domains: set[str]) -> bool:
        normalized = ContentGuardService._normalize_domain(domain)
        if not normalized:
            return False
        for allowed in allowed_domains:
            normalized_allowed = str(allowed or "").strip().lower()
            if normalized_allowed.startswith("*."):
                root = ContentGuardService._normalize_domain(normalized_allowed[2:])
                if root and normalized != root and normalized.endswith(f".{root}"):
                    return True
                continue
            root = ContentGuardService._normalize_domain(normalized_allowed)
            if root and normalized == root:
                return True
        return False

    @staticmethod
    def _keyword_matches(sample: str, term: str) -> bool:
        if not sample or not term:
            return False
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]*", term, flags=re.IGNORECASE):
            return term in sample
        pattern = rf"(?<![a-z0-9_.:-]){re.escape(term)}(?![a-z0-9_.:-])"
        return re.search(pattern, sample, flags=re.IGNORECASE) is not None

    @classmethod
    def _has_short_link_domain(cls, domains: set[str]) -> bool:
        return any(cls._normalize_domain(domain) in cls.SHORT_LINK_DOMAINS for domain in domains)

    @classmethod
    def _has_ad_intent(cls, text: str) -> bool:
        lower_text = cls.normalize_scan_text(text).lower()
        return any(term.lower() in lower_text for term in cls.AD_INTENT_TERMS)

    @staticmethod
    def _combine_rule_confidence(rules: list[ContentGuardRule]) -> float:
        if not rules:
            return 0.0
        clean_values = [max(0.0, min(1.0, float(rule.confidence or 0.0))) for rule in rules]
        miss_probability = 1.0
        for value in clean_values:
            miss_probability *= 1.0 - value
        return max(clean_values + [1.0 - miss_probability])

    @classmethod
    def _tail_risk_boost(cls, sample: str, rules: list[ContentGuardRule]) -> float:
        if not sample or not rules:
            return 0.0
        tail_start = max(0, int(len(sample) * 0.7))
        tail = sample[tail_start:].lower()
        if not tail:
            return 0.0
        for rule in rules:
            for pattern in rule.patterns:
                term = str(pattern).strip().lower()
                if not term:
                    continue
                if rule.match_type == "keyword_any" and cls._keyword_matches(tail, term):
                    return 0.08
                if rule.match_type == "regex":
                    try:
                        if re.search(term, tail, re.IGNORECASE):
                            return 0.08
                    except re.error:
                        continue
        return 0.0

    @staticmethod
    def should_block(result: ContentGuardResult, *, setting: Any | None = None) -> bool:
        if result.result not in {ContentGuardService.RESULT_BLOCK, ContentGuardService.RESULT_ERROR}:
            return False
        if setting is None:
            return True
        strategy = str(getattr(setting, "content_guard_high_risk_strategy", "") or "").strip()
        if strategy == "record_only":
            return False
        if strategy in {"switch_provider", "safe_error"}:
            return True
        raw_threshold = float(getattr(setting, "content_guard_high_risk_confidence_threshold", 85) or 85)
        threshold = raw_threshold / 100 if raw_threshold > 1 else raw_threshold
        if float(result.confidence or 0.0) < threshold:
            return False
        return bool(getattr(setting, "content_guard_block_on_high_risk", True))

    @classmethod
    def record_violation(
        cls,
        db: Session,
        *,
        provider: Provider | None,
        provider_model: ProviderModel | None,
        result: ContentGuardResult,
        severe: bool | None = None,
        source: str = "runtime",
        auto_commit: bool = True,
    ) -> None:
        if result.result not in {cls.RESULT_REVIEW, cls.RESULT_BLOCK, cls.RESULT_ERROR}:
            return
        now = now_beijing()
        final_strategy = str(getattr(result, "final_strategy", "") or getattr(result, "action", "") or "")
        if final_strategy in {"record_only", "record", "async_review"}:
            severe = False
        is_severe = result.result in {cls.RESULT_BLOCK, cls.RESULT_ERROR} if severe is None else severe
        if provider is not None:
            provider.content_violation_count = int(provider.content_violation_count or 0) + 1
            provider.last_content_violation_at = now
            if is_severe:
                provider.content_integrity_status = "blocked"
                provider.content_integrity_score = max(0, int(provider.content_integrity_score or 80) + int(result.score_delta or -10))
        if provider_model is not None and is_severe:
            if source == "probe":
                provider_model.content_probe_last_failed_at = now
                provider_model.content_probe_failure_count = int(provider_model.content_probe_failure_count or 0) + 1
            provider_model.content_integrity_status = "blocked"
        if is_severe and provider is not None:
            cls._upsert_content_guard_alert_event(
                db,
                provider=provider,
                provider_model=provider_model,
                result=result,
                occurred_at=now,
            )
        ProviderService.invalidate_provider_runtime_cache()
        if auto_commit:
            db.commit()

    @classmethod
    def _upsert_content_guard_alert_event(
        cls,
        db: Session,
        *,
        provider: Provider,
        provider_model: ProviderModel | None,
        result: ContentGuardResult,
        occurred_at: datetime,
    ) -> None:
        alert_key = f"runtime:content_guard_violation:{provider.id}"
        item = db.scalar(select(AlertEvent).where(AlertEvent.alert_key == alert_key))
        payload = {
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider_model_id": getattr(provider_model, "id", None),
            "model_name": getattr(provider_model, "model_name", None),
            "content_guard_result": result.result,
            "risk_level": result.risk_level,
            "categories": result.categories,
            "reason": result.reason,
            "provider_status_after": provider.content_integrity_status,
            "provider_circuit_state": provider.circuit_state,
            "provider_circuit_opened_at": provider.circuit_opened_at.isoformat()
            if getattr(provider, "circuit_opened_at", None)
            else None,
            "model_circuit_opened_at": provider_model.circuit_opened_at.isoformat()
            if provider_model is not None and provider_model.circuit_opened_at
            else None,
        }
        title = f"提供商内容完整性高风险 · {provider.name}"
        message = f"运行时内容防护命中高风险规则：{result.reason or result.risk_level}"
        if item is None:
            db.add(
                AlertEvent(
                    alert_key=alert_key,
                    alert_type="provider",
                    severity="danger",
                    title=title,
                    message=message,
                    payload_json=dumps_json(payload),
                    status="active",
                    first_seen_at=occurred_at,
                    last_seen_at=occurred_at,
                )
            )
            return
        item.alert_type = "provider"
        item.severity = "danger"
        item.title = title
        item.message = message
        item.payload_json = dumps_json(payload)
        item.status = "active"
        item.last_seen_at = occurred_at
        item.resolved_at = None

    @classmethod
    def _inspect_response_structure(
        cls,
        payload: Any,
        *,
        endpoint_path: str | None,
        request_payload: dict[str, Any] | None = None,
        max_scan_bytes: int = 16384,
        rules_json: str | None = None,
        url_allowlist: list[str] | str | None = None,
        url_check_enabled: bool = True,
        rules: list[ContentGuardRule | dict[str, Any]] | None = None,
        enhanced_detection_enabled: bool = True,
        enhanced_illegal_enabled: bool = True,
        enhanced_ad_enabled: bool = True,
        enhanced_custom_enabled: bool = True,
        enhanced_obfuscation_enabled: bool = True,
        enhanced_threshold: int = 70,
        enhanced_context_window_chars: int = 96,
    ) -> ContentGuardResult:
        if not isinstance(payload, dict):
            return ContentGuardResult(
                result=cls.RESULT_BLOCK,
                risk_level="high",
                categories=["invalid_json_structure"],
                reason="上游非流式响应不是 JSON 对象",
                action="block",
                excerpt=cls._excerpt(str(payload)),
                score_delta=-20,
            )
        if cls._has_meaningful_error_payload(payload):
            return cls._response_schema_violation(
                category="upstream_error_payload",
                reason="上游 HTTP 200 响应包含 error 字段，不能作为正常模型响应放行",
                payload=payload,
            )
        if endpoint_path == "/chat/completions" and not cls._has_non_empty_chat_choices(payload):
            return cls._response_schema_violation(
                category="chat_completion_schema_violation",
                reason="Chat Completions 响应缺少非空 choices 字段",
                payload=payload,
            )
        if endpoint_path == "/responses" and not cls._has_non_empty_responses_output(payload):
            return cls._response_schema_violation(
                category="responses_schema_violation",
                reason="Responses 响应缺少非空 output 或 output_text 字段",
                payload=payload,
            )
        suspicious_scan_root = cls._suspicious_response_scan_root(payload, endpoint_path=endpoint_path)
        suspicious_key = cls._find_suspicious_response_key(suspicious_scan_root)
        if (
            suspicious_key
            and cls._suspicious_key_has_polluting_value(suspicious_scan_root, suspicious_key)
            and not cls._request_allows_advertising(request_payload)
        ):
            return ContentGuardResult(
                result=cls.RESULT_BLOCK,
                risk_level="high",
                categories=["unexpected_advertising_field"],
                reason=f"上游响应包含疑似广告或导流字段：{suspicious_key}",
                action="block",
                excerpt=cls._excerpt(dumps_json({"key": suspicious_key})),
                score_delta=-20,
            )
        if cls._request_expects_json_response(request_payload):
            json_mode_result = cls._inspect_structured_json_mode(
                payload,
                endpoint_path=endpoint_path,
                request_payload=request_payload,
            )
            if json_mode_result.result == cls.RESULT_BLOCK:
                return json_mode_result
        tool_result = cls._inspect_tool_call_arguments(
            payload,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            max_scan_bytes=max_scan_bytes,
            rules_json=rules_json,
            url_allowlist=url_allowlist,
            url_check_enabled=url_check_enabled,
            rules=rules,
            enhanced_detection_enabled=enhanced_detection_enabled,
            enhanced_illegal_enabled=enhanced_illegal_enabled,
            enhanced_ad_enabled=enhanced_ad_enabled,
            enhanced_custom_enabled=enhanced_custom_enabled,
            enhanced_obfuscation_enabled=enhanced_obfuscation_enabled,
            enhanced_threshold=enhanced_threshold,
            enhanced_context_window_chars=enhanced_context_window_chars,
        )
        if tool_result.result == cls.RESULT_BLOCK:
            return tool_result
        return ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="响应结构通过")

    @classmethod
    def _response_schema_violation(cls, *, category: str, reason: str, payload: dict[str, Any]) -> ContentGuardResult:
        return ContentGuardResult(
            result=cls.RESULT_BLOCK,
            risk_level="high",
            categories=[category],
            reason=reason,
            action="block",
            excerpt=cls._excerpt(dumps_json({"keys": list(payload.keys())[:20]})),
            score_delta=-20,
        )

    @staticmethod
    def _has_meaningful_error_payload(payload: dict[str, Any]) -> bool:
        error = payload.get("error")
        if error is None or error is False:
            return False
        if isinstance(error, str):
            return bool(error.strip())
        if isinstance(error, (list, tuple, set)):
            return bool(error)
        if isinstance(error, dict):
            return bool(error)
        return True

    @staticmethod
    def _has_non_empty_chat_choices(payload: dict[str, Any]) -> bool:
        choices = payload.get("choices")
        return isinstance(choices, list) and len(choices) > 0

    @staticmethod
    def _has_non_empty_responses_output(payload: dict[str, Any]) -> bool:
        output = payload.get("output")
        if isinstance(output, list) and len(output) > 0:
            return True
        output_text = payload.get("output_text")
        return isinstance(output_text, str) and bool(output_text.strip())

    @classmethod
    def _suspicious_response_scan_root(cls, payload: dict[str, Any], *, endpoint_path: str | None) -> Any:
        if endpoint_path not in cls.STRUCTURED_ENDPOINTS:
            return {}
        roots: list[Any] = []
        top_level_suspicious = {
            key: nested
            for key, nested in payload.items()
            if re.sub(r"[^a-z0-9_]+", "", str(key).lower()) in cls.SUSPICIOUS_RESPONSE_KEYS
        }
        if top_level_suspicious:
            roots.append(top_level_suspicious)
        if endpoint_path in {"/chat/completions", "/completions"}:
            for choice in payload.get("choices") or []:
                if isinstance(choice, dict):
                    roots.append(choice)
        elif endpoint_path == "/responses":
            for item in payload.get("output") or []:
                if isinstance(item, dict):
                    roots.append(item)
                    for content in item.get("content") or []:
                        if isinstance(content, dict):
                            roots.append(content)
        return {"response_items": roots}

    @classmethod
    def _inspect_structured_json_mode(
        cls,
        payload: dict[str, Any],
        *,
        endpoint_path: str | None,
        request_payload: dict[str, Any] | None,
    ) -> ContentGuardResult:
        texts: list[str] = []
        if endpoint_path == "/chat/completions":
            for choice in payload.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") or {}
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    texts.append(message["content"])
        elif endpoint_path == "/responses":
            for item in payload.get("output") or []:
                if not isinstance(item, dict):
                    continue
                for content in item.get("content") or []:
                    if not isinstance(content, dict):
                        continue
                    text = content.get("text") or content.get("output_text")
                    if isinstance(text, str):
                        texts.append(text)
        combined = "\n".join(part.strip() for part in texts if part and part.strip()).strip()
        if not combined:
            return ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="JSON 模式未发现文本内容")
        try:
            parsed = json.loads(combined)
        except Exception:
            return ContentGuardResult(
                result=cls.RESULT_BLOCK,
                risk_level="high",
                categories=["structured_json_pollution"],
                reason="结构化 JSON 输出夹带自然语言或非 JSON 内容",
                action="block",
                excerpt=cls._excerpt(combined),
                score_delta=-20,
            )
        schema = cls._structured_json_schema_from_request(request_payload)
        if isinstance(schema, dict):
            schema_error = cls._validate_json_schema_subset(parsed, schema)
            if schema_error:
                return ContentGuardResult(
                    result=cls.RESULT_BLOCK,
                    risk_level="high",
                    categories=["structured_json_schema_violation"],
                    reason=f"结构化 JSON 输出不符合请求 schema：{schema_error}",
                    action="block",
                    excerpt=cls._excerpt(dumps_json(parsed)),
                    score_delta=-20,
                    confidence=0.9,
                )
        return ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="结构化 JSON 输出通过")

    @classmethod
    def _structured_json_schema_from_request(cls, request_payload: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(request_payload, dict):
            return None
        response_format = request_payload.get("response_format")
        if isinstance(response_format, dict):
            schema_container = response_format.get("json_schema")
            if isinstance(schema_container, dict) and isinstance(schema_container.get("schema"), dict):
                return schema_container["schema"]
            if isinstance(response_format.get("schema"), dict):
                return response_format["schema"]
        text = request_payload.get("text")
        if isinstance(text, dict):
            text_format = text.get("format")
            if isinstance(text_format, dict):
                if isinstance(text_format.get("schema"), dict):
                    return text_format["schema"]
                schema_container = text_format.get("json_schema")
                if isinstance(schema_container, dict) and isinstance(schema_container.get("schema"), dict):
                    return schema_container["schema"]
        return None

    @classmethod
    def _validate_json_schema_subset(cls, value: Any, schema: dict[str, Any], *, path: str = "$", depth: int = 0) -> str | None:
        if depth > 16:
            return None
        if not isinstance(schema, dict):
            return None
        for key in ("anyOf", "oneOf"):
            variants = schema.get(key)
            if isinstance(variants, list) and variants:
                errors = [
                    cls._validate_json_schema_subset(value, item, path=path, depth=depth + 1)
                    for item in variants
                    if isinstance(item, dict)
                ]
                if any(error is None for error in errors):
                    return None
                return errors[0] if errors else None
        all_of = schema.get("allOf")
        if isinstance(all_of, list):
            for item in all_of:
                if isinstance(item, dict):
                    error = cls._validate_json_schema_subset(value, item, path=path, depth=depth + 1)
                    if error:
                        return error
        if "const" in schema and value != schema.get("const"):
            return f"{path} 不等于 const 指定值"
        enum_values = schema.get("enum")
        if isinstance(enum_values, list) and value not in enum_values:
            return f"{path} 不在 enum 允许值中"
        expected_type = schema.get("type")
        if isinstance(expected_type, list):
            type_errors = [
                cls._validate_json_schema_subset(value, {**schema, "type": item}, path=path, depth=depth + 1)
                for item in expected_type
            ]
            if any(error is None for error in type_errors):
                return None
            return type_errors[0] if type_errors else None
        if isinstance(expected_type, str):
            type_error = cls._json_schema_type_error(value, expected_type, path)
            if type_error:
                return type_error
        if expected_type == "object" or isinstance(schema.get("properties"), dict):
            if not isinstance(value, dict):
                return f"{path} 必须是对象"
            properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            for key in required:
                if isinstance(key, str) and key not in value:
                    return f"{path}.{key} 为必填字段"
            additional = schema.get("additionalProperties")
            if additional is False:
                extra_keys = sorted(set(value.keys()) - {str(key) for key in properties.keys()})
                if extra_keys:
                    return f"{path} 存在未声明字段 {extra_keys[0]}"
            for key, nested_schema in properties.items():
                if isinstance(key, str) and key in value and isinstance(nested_schema, dict):
                    error = cls._validate_json_schema_subset(value[key], nested_schema, path=f"{path}.{key}", depth=depth + 1)
                    if error:
                        return error
        if expected_type == "array" or isinstance(schema.get("items"), dict):
            if not isinstance(value, list):
                return f"{path} 必须是数组"
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value[:100]):
                    error = cls._validate_json_schema_subset(item, item_schema, path=f"{path}[{index}]", depth=depth + 1)
                    if error:
                        return error
        return None

    @staticmethod
    def _json_schema_type_error(value: Any, expected_type: str, path: str) -> str | None:
        checks = {
            "object": lambda item: isinstance(item, dict),
            "array": lambda item: isinstance(item, list),
            "string": lambda item: isinstance(item, str),
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "boolean": lambda item: isinstance(item, bool),
            "null": lambda item: item is None,
        }
        checker = checks.get(expected_type)
        if checker is None or checker(value):
            return None
        return f"{path} 类型必须是 {expected_type}"

    @classmethod
    def _inspect_tool_call_arguments(
        cls,
        payload: dict[str, Any],
        *,
        endpoint_path: str | None,
        request_payload: dict[str, Any] | None = None,
        max_scan_bytes: int = 16384,
        rules_json: str | None = None,
        url_allowlist: list[str] | str | None = None,
        url_check_enabled: bool = True,
        rules: list[ContentGuardRule | dict[str, Any]] | None = None,
        enhanced_detection_enabled: bool = True,
        enhanced_illegal_enabled: bool = True,
        enhanced_ad_enabled: bool = True,
        enhanced_custom_enabled: bool = True,
        enhanced_obfuscation_enabled: bool = True,
        enhanced_threshold: int = 70,
        enhanced_context_window_chars: int = 96,
    ) -> ContentGuardResult:
        arguments: list[Any] = []
        if endpoint_path == "/chat/completions":
            for choice in payload.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") or {}
                if not isinstance(message, dict):
                    continue
                for tool_call in message.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function") or {}
                    if isinstance(function, dict) and "arguments" in function:
                        arguments.append(function.get("arguments"))
                function_call = message.get("function_call")
                if isinstance(function_call, dict) and "arguments" in function_call:
                    arguments.append(function_call.get("arguments"))
        elif endpoint_path == "/responses":
            for item in payload.get("output") or []:
                if isinstance(item, dict):
                    arguments.extend(cls._collect_tool_call_arguments(item))
        for value in arguments:
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except Exception:
                    return ContentGuardResult(
                        result=cls.RESULT_BLOCK,
                        risk_level="high",
                        categories=["tool_argument_pollution"],
                        reason="工具调用参数不是合法 JSON，疑似被自然语言或广告污染",
                        action="block",
                        excerpt=cls._excerpt(value),
                        score_delta=-20,
                    )
            else:
                parsed = value
            if not isinstance(parsed, (dict, list)):
                return ContentGuardResult(
                    result=cls.RESULT_BLOCK,
                    risk_level="high",
                    categories=["tool_argument_pollution"],
                    reason="工具调用参数不是 JSON 对象或数组",
                    action="block",
                    excerpt=cls._excerpt(dumps_json(parsed)),
                    score_delta=-20,
                )
            text = cls._extract_scan_text(parsed, max_scan_bytes=max_scan_bytes)
            if text:
                text_result = cls.inspect_response_text(
                    text,
                    request_payload=request_payload,
                    max_scan_bytes=max_scan_bytes,
                    rules_json=rules_json,
                    url_allowlist=url_allowlist,
                    url_check_enabled=url_check_enabled,
                    rules=rules,
                    enhanced_detection_enabled=enhanced_detection_enabled,
                    enhanced_illegal_enabled=enhanced_illegal_enabled,
                    enhanced_ad_enabled=enhanced_ad_enabled,
                    enhanced_custom_enabled=enhanced_custom_enabled,
                    enhanced_obfuscation_enabled=enhanced_obfuscation_enabled,
                    enhanced_threshold=enhanced_threshold,
                    enhanced_context_window_chars=enhanced_context_window_chars,
                )
                if text_result.result != cls.RESULT_PASS:
                    return ContentGuardResult(
                        result=cls.RESULT_BLOCK,
                        risk_level="high",
                        categories=["tool_argument_pollution", *text_result.categories],
                        reason=f"工具调用参数命中内容污染规则：{text_result.reason}",
                        action="block",
                        excerpt=text_result.excerpt,
                        score_delta=min(-20, int(text_result.score_delta or -20)),
                        confidence=max(0.85, float(text_result.confidence or 0.0)),
                    )
        return ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="工具调用参数通过")

    @classmethod
    def _collect_tool_call_arguments(cls, value: Any, *, depth: int = 0) -> list[Any]:
        if depth > 8:
            return []
        arguments: list[Any] = []
        if isinstance(value, dict):
            item_type = str(value.get("type") or value.get("item_type") or "")
            looks_like_tool_call = item_type in {"function_call", "tool_call", "response.function_call", "response.tool_call"}
            if looks_like_tool_call and "arguments" in value:
                arguments.append(value.get("arguments"))
            function = value.get("function")
            if isinstance(function, dict) and "arguments" in function:
                arguments.append(function.get("arguments"))
            call = value.get("call")
            if isinstance(call, dict) and "arguments" in call:
                arguments.append(call.get("arguments"))
            tool_call = value.get("tool_call")
            if isinstance(tool_call, dict):
                arguments.extend(cls._collect_tool_call_arguments(tool_call, depth=depth + 1))
            for nested_key in ("content", "output", "items", "delta"):
                nested = value.get(nested_key)
                if isinstance(nested, (dict, list)):
                    arguments.extend(cls._collect_tool_call_arguments(nested, depth=depth + 1))
        elif isinstance(value, list):
            for item in value:
                arguments.extend(cls._collect_tool_call_arguments(item, depth=depth + 1))
        return arguments

    @classmethod
    def _request_expects_json_response(cls, request_payload: dict[str, Any] | None) -> bool:
        if not isinstance(request_payload, dict):
            return False
        response_format = request_payload.get("response_format")
        if isinstance(response_format, dict) and str(response_format.get("type") or "") in cls.JSON_MODE_TYPES:
            return True
        text = request_payload.get("text")
        if isinstance(text, dict):
            text_format = text.get("format")
            if isinstance(text_format, dict) and str(text_format.get("type") or "") in cls.JSON_MODE_TYPES:
                return True
        return False

    @classmethod
    def _find_suspicious_response_key(cls, value: Any) -> str | None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = re.sub(r"[^a-z0-9_]+", "", str(key).lower())
                if normalized in cls.SUSPICIOUS_RESPONSE_KEYS:
                    return str(key)
                if normalized in cls.SUSPICIOUS_KEY_SCAN_SKIP_KEYS:
                    continue
                found = cls._find_suspicious_response_key(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = cls._find_suspicious_response_key(nested)
                if found:
                    return found
        return None

    @classmethod
    def _suspicious_key_has_polluting_value(cls, value: Any, key_name: str) -> bool:
        normalized_target = re.sub(r"[^a-z0-9_]+", "", str(key_name).lower())

        def find_value(item: Any) -> Any:
            if isinstance(item, dict):
                for key, nested in item.items():
                    normalized = re.sub(r"[^a-z0-9_]+", "", str(key).lower())
                    if normalized == normalized_target:
                        return nested
                    if normalized in cls.SUSPICIOUS_KEY_SCAN_SKIP_KEYS:
                        continue
                    found = find_value(nested)
                    if found is not None:
                        return found
            elif isinstance(item, list):
                for nested in item:
                    found = find_value(nested)
                    if found is not None:
                        return found
            return None

        suspicious_value = find_value(value)
        if suspicious_value is None:
            return False
        text = cls._extract_scan_text(suspicious_value, max_scan_bytes=4096)
        if not text:
            return False
        domains = cls.extract_domains(text)
        if domains or cls._has_ad_intent(text):
            return True
        return bool(
            re.search(r"(扫码|加群|联系|私聊|客服|优惠|返利|充值|注册|推广|赞助|广告|二维码)", text, flags=re.IGNORECASE)
        )

    @classmethod
    def _payload_contains_file_reference(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).lower()
                if normalized in {"file", "files", "file_id", "file_ids", "filename", "attachments", "file_data", "file_url"}:
                    return True
                if normalized in {"url", "uri"} and isinstance(nested, str) and cls._looks_like_file_reference(nested):
                    return True
                if normalized == "type" and isinstance(nested, str) and nested in {"input_file", "file_search"}:
                    return True
                if cls._payload_contains_file_reference(nested):
                    return True
        elif isinstance(value, list):
            return any(cls._payload_contains_file_reference(item) for item in value)
        return False

    @staticmethod
    def _looks_like_file_reference(value: str) -> bool:
        text = value.strip().lower()
        if not text:
            return False
        if text.startswith(("data:", "file:", "cid:")):
            return True
        parsed = urlparse(text)
        path = parsed.path if parsed.scheme else text
        return bool(re.search(r"\.(pdf|docx?|xlsx?|pptx?|csv|json|jsonl|txt|md|zip|tar|gz|7z|png|jpe?g|webp|gif|svg|mp3|mp4|wav|mov|avi)$", path))

    @classmethod
    def _payload_has_long_context(cls, payload: dict[str, Any]) -> bool:
        text = cls._extract_scan_text(payload, max_scan_bytes=32768)
        if len(text.encode("utf-8", errors="ignore")) >= 16000:
            return True
        messages = payload.get("messages")
        if isinstance(messages, list) and len(messages) >= 20:
            return True
        input_items = payload.get("input")
        if isinstance(input_items, list) and len(input_items) >= 20:
            return True
        if cls._payload_contains_file_reference(payload):
            return True
        nested_content_count = cls._count_nested_content_items(payload)
        if nested_content_count >= 80:
            return True
        return False

    @classmethod
    def _count_nested_content_items(cls, value: Any, *, depth: int = 0) -> int:
        if depth > cls.MAX_SCAN_DEPTH:
            return 0
        if isinstance(value, dict):
            count = 1 if "content" in value or "input" in value or "messages" in value else 0
            return count + sum(cls._count_nested_content_items(item, depth=depth + 1) for item in value.values())
        if isinstance(value, list):
            return len(value) + sum(cls._count_nested_content_items(item, depth=depth + 1) for item in value)
        return 0

    @classmethod
    def _extract_scan_text(cls, value: Any, *, max_scan_bytes: int) -> str:
        parts: list[str] = []
        current_bytes = 0
        visited_nodes = 0

        def walk(item: Any, *, depth: int = 0) -> None:
            nonlocal current_bytes, visited_nodes
            if current_bytes >= max_scan_bytes or visited_nodes >= cls.MAX_SCAN_NODES or depth > cls.MAX_SCAN_DEPTH:
                return
            visited_nodes += 1
            if isinstance(item, str):
                remaining = max_scan_bytes - current_bytes
                if remaining <= 0:
                    return
                encoded = item.encode("utf-8", errors="ignore")
                clipped = encoded[:remaining].decode("utf-8", errors="ignore")
                if clipped:
                    parts.append(clipped)
                    current_bytes += len(clipped.encode("utf-8", errors="ignore"))
                return
            if isinstance(item, dict):
                for key, nested in item.items():
                    if str(key).lower() in {"api_key", "authorization", "base64", "b64_json"}:
                        continue
                    walk(nested, depth=depth + 1)
                    if current_bytes >= max_scan_bytes or visited_nodes >= cls.MAX_SCAN_NODES:
                        break
            elif isinstance(item, list):
                for nested in item:
                    walk(nested, depth=depth + 1)
                    if current_bytes >= max_scan_bytes or visited_nodes >= cls.MAX_SCAN_NODES:
                        break

        walk(value)
        return "\n".join(parts)

    @classmethod
    def _extract_response_scan_text(cls, value: Any, *, endpoint_path: str | None, max_scan_bytes: int) -> str:
        if not isinstance(value, dict):
            return cls._extract_scan_text(value, max_scan_bytes=max_scan_bytes)
        parts: list[str] = []

        def add(text: Any) -> None:
            if isinstance(text, str) and text:
                parts.append(text)
            elif isinstance(text, (dict, list)):
                add_scan(text)

        def add_scan(value: Any) -> None:
            if value is None:
                return
            if isinstance(value, str):
                add(value)
                return
            extracted = cls._extract_scan_text(value, max_scan_bytes=max_scan_bytes)
            if extracted:
                parts.append(extracted)

        if endpoint_path == "/chat/completions":
            for choice in value.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                add(message.get("content"))
                add(delta.get("content"))
                add(message.get("refusal"))
                add(delta.get("refusal"))
                add_scan(message.get("image_url"))
                add_scan(delta.get("image_url"))
                for container in (message, delta):
                    for tool_call in container.get("tool_calls") or []:
                        if not isinstance(tool_call, dict):
                            continue
                        function = tool_call.get("function") or {}
                        if isinstance(function, dict):
                            add(function.get("arguments"))
                    function_call = container.get("function_call")
                    if isinstance(function_call, dict):
                        add(function_call.get("arguments"))
        elif endpoint_path == "/responses":
            for item in value.get("output") or []:
                if not isinstance(item, dict):
                    continue
                add(item.get("arguments"))
                add(item.get("delta"))
                add_scan(item.get("summary"))
                add_scan(item.get("reasoning"))
                add_scan(item.get("annotations"))
                add_scan(item.get("citations"))
                add_scan(item.get("refusal"))
                add_scan(item.get("metadata"))
                for content in item.get("content") or []:
                    if isinstance(content, dict):
                        add(content.get("text"))
                        add(content.get("output_text"))
                        add(content.get("delta"))
                        add_scan(content.get("summary"))
                        add_scan(content.get("reasoning"))
                        add_scan(content.get("annotations"))
                        add_scan(content.get("citations"))
                        add_scan(content.get("refusal"))
                        add_scan(content.get("metadata"))
                        add_scan(content.get("image_url"))
                    elif isinstance(content, (str, list)):
                        add_scan(content)
            add(value.get("output_text"))
            add_scan(value.get("annotations"))
            add_scan(value.get("citations"))
            add_scan(value.get("refusal"))
            add_scan(value.get("reasoning"))
            add_scan(value.get("metadata"))
        if not parts:
            return cls._extract_scan_text(value, max_scan_bytes=max_scan_bytes)
        return cls._clip_text("\n".join(parts), max_scan_bytes=max_scan_bytes)

    @classmethod
    def _clip_text(cls, text: str | None, *, max_scan_bytes: int) -> str:
        if not text:
            return ""
        raw = str(text)
        encoded = raw.encode("utf-8", errors="ignore")
        if len(encoded) <= max_scan_bytes:
            return raw
        return encoded[:max_scan_bytes].decode("utf-8", errors="ignore")

    @classmethod
    def _excerpt(cls, text: str | None) -> str | None:
        if not text:
            return None
        normalized = re.sub(r"\s+", " ", str(text)).strip()
        if len(normalized) <= cls.EXCERPT_MAX_CHARS:
            return normalized
        return normalized[: cls.EXCERPT_MAX_CHARS] + "..."

    @staticmethod
    def _request_allows_advertising(request_payload: dict[str, Any] | None) -> bool:
        text = ContentGuardService._extract_scan_text(request_payload or {}, max_scan_bytes=8192).lower()
        if ContentGuardService._request_disallows_advertising_or_links(request_payload):
            return False
        explicit_allow_patterns = (
            r"(使用|引用|包含|加入|附带|提供|保留).{0,16}(https?://|[a-z0-9][a-z0-9.-]+\.[a-z]{2,63})",
            r"(https?://|[a-z0-9][a-z0-9.-]+\.[a-z]{2,63}).{0,16}(使用|引用|包含|加入|附带|提供|保留)",
            r"(广告|推广|营销|落地页|优惠|赞助|活动页|campaign).{0,32}(链接|外链|广告链接|优惠链接|开户链接)",
            r"(链接|外链|广告链接|优惠链接|开户链接).{0,32}(广告|推广|营销|落地页|优惠|赞助|活动页|campaign)",
        )
        return any(re.search(pattern, text) for pattern in explicit_allow_patterns)

    @staticmethod
    def _request_disallows_advertising_or_links(request_payload: dict[str, Any] | None) -> bool:
        text = ContentGuardService._extract_scan_text(request_payload or {}, max_scan_bytes=8192).lower()
        return bool(
            re.search(r"(不要|禁止|不得|不能|避免|不允许|无|去除|移除|不要提供).{0,16}(广告|推广|营销|落地页|优惠|链接|联系方式|外链)", text)
            or re.search(r"(广告|推广|营销|落地页|优惠|链接|联系方式|外链).{0,16}(不要|禁止|不得|不能|避免|不允许|无|去除|移除)", text)
        )

from app.utils.timezone import now_beijing