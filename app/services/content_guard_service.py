from __future__ import annotations

import json
import re
import html
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.provider_service import ProviderService
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

    _URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
    _URL_MARKERS = ("http://", "https://")
    _ALLOWED_RULE_MATCH_TYPES = {"keyword_any", "regex", "unexpected_url"}
    _ALLOWED_RULE_RISK_LEVELS = {"low", "medium", "high"}
    _ALLOWED_RULE_ACTIONS = {"allow", "record", "block"}
    _ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
    _DOMAIN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\b", re.IGNORECASE)
    DEFAULT_URL_ALLOWLIST = {
        "openai.com",
        "platform.openai.com",
        "github.com",
        "docs.github.com",
        "microsoft.com",
        "learn.microsoft.com",
        "google.com",
        "cloud.google.com",
        "anthropic.com",
    }
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
                r"(微信|wechat|qq|telegram|whatsapp).{0,8}(号|群|联系|私聊|客服|添加)",
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
        """判断请求是否应优先走可信提供商，不执行健康探测。"""
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
    ) -> ContentGuardResult:
        started = datetime.utcnow()
        sample = cls._clip_text(cls.normalize_scan_text(text), max_scan_bytes=max_scan_bytes)
        if not sample:
            return ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="未检测到可扫描文本")
        matched_rules = cls.match_text_rules(
            sample,
            request_payload=request_payload,
            url_allowlist=url_allowlist,
            url_check_enabled=url_check_enabled,
            rules=rules if rules is not None else cls.parse_rules_json(rules_json),
        )
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
        categories = []
        for rule in matched_rules:
            if rule.category not in categories:
                categories.append(rule.category)
        high_risk = any(rule.risk_level == "high" or rule.action == "block" for rule in matched_rules)
        score_total = sum(abs(int(rule.score_delta or 0)) for rule in matched_rules)
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
    ) -> list[ContentGuardRule]:
        normalized_rules = cls.normalize_rules(rules)
        normalized_text = cls.normalize_scan_text(text)
        lower_sample = normalized_text.lower()
        response_domains = cls.extract_domains(normalized_text)
        allowed_domains = cls.allowed_domains(request_payload=request_payload, url_allowlist=url_allowlist)
        unexpected_domains = {domain for domain in response_domains if not cls._domain_allowed(domain, allowed_domains)}
        matched: list[ContentGuardRule] = []
        for rule in normalized_rules:
            if not rule.enabled:
                continue
            if rule.match_type == "unexpected_url":
                if url_check_enabled and unexpected_domains and not cls._request_allows_advertising(request_payload):
                    matched.append(rule)
                continue
            if rule.match_type == "keyword_any":
                terms = [str(item).strip().lower() for item in rule.patterns if str(item).strip()]
                if terms and any(term in lower_sample for term in terms):
                    matched.append(rule)
                continue
            if rule.match_type == "regex":
                for pattern in rule.patterns:
                    try:
                        if re.search(str(pattern), normalized_text, re.IGNORECASE):
                            matched.append(rule)
                            break
                    except re.error as exc:
                        matched.append(
                            ContentGuardRule(
                                id=f"invalid_regex_{rule.id}",
                                name=f"无效正则：{rule.name}",
                                category="rule_configuration_error",
                                match_type="regex",
                                patterns=[str(pattern)],
                                risk_level="high",
                                action="record",
                                score_delta=0,
                                confidence=1.0,
                                reason=f"内容防护正则规则 `{rule.name}` 无效：{exc}",
                            )
                        )
                        break
        if url_check_enabled and cls._has_short_link_domain(response_domains):
            matched.append(
                ContentGuardRule(
                    id="short_link_redirect",
                    name="短链导流",
                    category="unexpected_link",
                    match_type="unexpected_url",
                    patterns=[],
                    risk_level="high",
                    action="block",
                    score_delta=-25,
                    confidence=0.86,
                    reason="响应包含短链域名",
                )
            )
        if url_check_enabled and unexpected_domains and cls._has_ad_intent(normalized_text) and not cls._request_allows_advertising(request_payload):
            matched.append(
                ContentGuardRule(
                    id="unexpected_link_with_ad_intent",
                    name="外链广告引导",
                    category="advertising_or_promotion",
                    match_type="unexpected_url",
                    patterns=[],
                    risk_level="high",
                    action="block",
                    score_delta=-25,
                    confidence=0.88,
                    reason="响应包含未授权外链并伴随广告、注册、优惠或导流意图",
                )
            )
        return matched

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
    ) -> ContentGuardResult:
        started = datetime.utcnow()
        structure_result = cls._inspect_response_structure(payload, endpoint_path=endpoint_path, request_payload=request_payload)
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
    ) -> ContentGuardResult:
        started = datetime.utcnow()
        if not data or data == "[DONE]":
            return cls._with_latency(
                ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="SSE 控制事件"),
                started=started,
            )
        stripped = data.strip()
        if not stripped.startswith("{"):
            return cls._with_latency(
                ContentGuardResult(
                    result=cls.RESULT_BLOCK,
                    risk_level="high",
                    categories=["invalid_sse_event"],
                    reason="SSE data 不是合法 JSON 事件",
                    action="block",
                    excerpt=cls._excerpt(stripped),
                    score_delta=-20,
                ),
                started=started,
            )
        try:
            payload = json.loads(stripped)
        except Exception:
            return cls._with_latency(
                ContentGuardResult(
                    result=cls.RESULT_BLOCK,
                    risk_level="high",
                    categories=["invalid_sse_event"],
                    reason="SSE data 不是合法 JSON 事件",
                    action="block",
                    excerpt=cls._excerpt(stripped),
                    score_delta=-20,
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
        )

    @classmethod
    def default_rules(cls) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in cls.DEFAULT_TEXT_RULES]

    @classmethod
    def parse_rules_json(cls, rules_json: str | None) -> list[ContentGuardRule]:
        if not rules_json:
            return cls.normalize_rules(None)
        try:
            parsed = json.loads(rules_json)
        except Exception:
            return cls.normalize_rules(None)
        return cls.normalize_rules(parsed)

    @classmethod
    def serialize_rules_json(cls, rules: list[ContentGuardRule | dict[str, Any]] | None) -> str:
        return dumps_json([rule.to_dict() if isinstance(rule, ContentGuardRule) else rule for rule in cls.normalize_rules(rules)])

    @classmethod
    def normalize_rules(cls, rules: list[ContentGuardRule | dict[str, Any]] | None) -> list[ContentGuardRule]:
        use_defaults = rules is None
        source = rules if isinstance(rules, list) else list(cls.DEFAULT_TEXT_RULES)
        normalized: list[ContentGuardRule] = []
        seen: set[str] = set()
        for index, item in enumerate(source):
            rule = item if isinstance(item, ContentGuardRule) else cls._coerce_rule(item, index=index)
            if rule is None or rule.id in seen:
                continue
            seen.add(rule.id)
            normalized.append(rule)
        return normalized or (list(cls.DEFAULT_TEXT_RULES) if use_defaults else [])

    @classmethod
    def _coerce_rule(cls, item: Any, *, index: int) -> ContentGuardRule | None:
        if not isinstance(item, dict):
            return None
        rule_id = cls._normalize_rule_id(str(item.get("id") or f"custom_rule_{index + 1}"))
        name = str(item.get("name") or rule_id).strip()[:80]
        category = cls._normalize_rule_id(str(item.get("category") or rule_id))
        match_type = str(item.get("match_type") or "keyword_any").strip()
        risk_level = str(item.get("risk_level") or "medium").strip()
        action = str(item.get("action") or "record").strip()
        patterns = item.get("patterns")
        if isinstance(patterns, str):
            patterns = [line.strip() for line in patterns.splitlines() if line.strip()]
        if not isinstance(patterns, list):
            patterns = []
        clean_patterns = [str(pattern).strip()[:500] for pattern in patterns if str(pattern).strip()]
        if not rule_id or not name or not category or not clean_patterns:
            return None
        if match_type not in cls._ALLOWED_RULE_MATCH_TYPES:
            match_type = "keyword_any"
        if risk_level not in cls._ALLOWED_RULE_RISK_LEVELS:
            risk_level = "medium"
        if action not in cls._ALLOWED_RULE_ACTIONS:
            action = "record"
        try:
            score_delta = int(item.get("score_delta", -8))
        except Exception:
            score_delta = -8
        try:
            confidence = float(item.get("confidence", 0.7))
        except Exception:
            confidence = 0.7
        confidence = max(0.0, min(1.0, confidence))
        return ContentGuardRule(
            id=rule_id,
            name=name,
            category=category,
            enabled=bool(item.get("enabled", True)),
            match_type=match_type,
            patterns=clean_patterns[:50],
            risk_level=risk_level,
            action=action,
            score_delta=max(-100, min(0, score_delta)),
            confidence=confidence,
            reason=str(item.get("reason") or "").strip()[:200],
        )

    @staticmethod
    def _normalize_rule_id(value: str) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")[:64]

    @staticmethod
    def _summarize_matched_rules(rules: list[ContentGuardRule]) -> str:
        names = [rule.name for rule in rules[:3] if rule.name]
        if not names:
            return ""
        return "命中内容防护规则：" + "、".join(names)

    @staticmethod
    def _with_latency(result: ContentGuardResult, *, started: datetime) -> ContentGuardResult:
        result.latency_ms = max(0, int((datetime.utcnow() - started).total_seconds() * 1000))
        return result

    @classmethod
    def normalize_scan_text(cls, text: str | None) -> str:
        if not isinstance(text, str) or not text:
            return ""
        normalized = unicodedata.normalize("NFKC", text)
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
        domains = set(cls.DEFAULT_URL_ALLOWLIST)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
                else:
                    value = [item.strip() for item in value.replace(",", "\n").splitlines()]
            except Exception:
                value = [item.strip() for item in value.replace(",", "\n").splitlines()]
        if isinstance(value, list):
            for item in value:
                domain = cls._normalize_domain(str(item))
                if domain:
                    domains.add(domain)
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
        return domains

    @staticmethod
    def _normalize_domain(value: str) -> str:
        domain = value.strip().lower().rstrip(".")
        if not domain:
            return ""
        if "://" in domain:
            domain = urlparse(domain).hostname or ""
        domain = domain.removeprefix("www.").strip().lower().rstrip(".")
        if not domain or "." not in domain:
            return ""
        return domain[:253]

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
            return True
        return any(normalized == allowed or normalized.endswith(f".{allowed}") for allowed in allowed_domains)

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
                if rule.match_type == "keyword_any" and term in tail:
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
        if result.result != ContentGuardService.RESULT_BLOCK:
            return False
        if setting is None:
            return True
        strategy = str(getattr(setting, "content_guard_high_risk_strategy", "") or "").strip()
        if strategy == "record_only":
            return False
        raw_threshold = float(getattr(setting, "content_guard_high_risk_confidence_threshold", 85) or 85)
        threshold = raw_threshold / 100 if raw_threshold > 1 else raw_threshold
        if float(result.confidence or 0.0) < threshold and result.action != "block":
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
        auto_commit: bool = True,
    ) -> None:
        if result.result not in {cls.RESULT_REVIEW, cls.RESULT_BLOCK, cls.RESULT_ERROR}:
            return
        now = datetime.utcnow()
        is_severe = result.result == cls.RESULT_BLOCK if severe is None else severe
        if provider is not None:
            provider.content_violation_count = int(provider.content_violation_count or 0) + 1
            provider.last_content_violation_at = now
            provider.content_integrity_status = "blocked" if is_severe else "degraded"
            provider.content_integrity_score = max(0, int(provider.content_integrity_score or 80) + int(result.score_delta or -10))
            if is_severe and getattr(provider, "auto_circuit_break_enabled", True):
                provider.circuit_state = "open"
        if provider_model is not None:
            provider_model.content_probe_last_failed_at = now
            provider_model.content_probe_failure_count = int(provider_model.content_probe_failure_count or 0) + 1
            provider_model.content_integrity_status = "blocked" if is_severe else "degraded"
            if is_severe:
                provider_model.circuit_state = "open"
                provider_model.circuit_opened_at = now
        ProviderService.invalidate_provider_runtime_cache()
        if auto_commit:
            db.commit()

    @classmethod
    def _inspect_response_structure(
        cls,
        payload: Any,
        *,
        endpoint_path: str | None,
        request_payload: dict[str, Any] | None = None,
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
        if endpoint_path == "/chat/completions" and "choices" not in payload and "error" not in payload:
            return ContentGuardResult(
                result=cls.RESULT_BLOCK,
                risk_level="high",
                categories=["chat_completion_schema_violation"],
                reason="Chat Completions 响应缺少 choices 字段",
                action="block",
                excerpt=cls._excerpt(dumps_json({"keys": list(payload.keys())[:20]})),
                score_delta=-20,
            )
        if endpoint_path == "/responses" and "output" not in payload and "error" not in payload:
            return ContentGuardResult(
                result=cls.RESULT_BLOCK,
                risk_level="high",
                categories=["responses_schema_violation"],
                reason="Responses 响应缺少 output 字段",
                action="block",
                excerpt=cls._excerpt(dumps_json({"keys": list(payload.keys())[:20]})),
                score_delta=-20,
            )
        suspicious_key = cls._find_suspicious_response_key(payload)
        if suspicious_key and not cls._request_allows_advertising(request_payload):
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
            json_mode_result = cls._inspect_structured_json_mode(payload, endpoint_path=endpoint_path)
            if json_mode_result.result == cls.RESULT_BLOCK:
                return json_mode_result
        tool_result = cls._inspect_tool_call_arguments(payload, endpoint_path=endpoint_path, request_payload=request_payload)
        if tool_result.result == cls.RESULT_BLOCK:
            return tool_result
        return ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="响应结构通过")

    @classmethod
    def _inspect_structured_json_mode(cls, payload: dict[str, Any], *, endpoint_path: str | None) -> ContentGuardResult:
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
            json.loads(combined)
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
        return ContentGuardResult(result=cls.RESULT_PASS, risk_level="low", reason="结构化 JSON 输出通过")

    @classmethod
    def _inspect_tool_call_arguments(
        cls,
        payload: dict[str, Any],
        *,
        endpoint_path: str | None,
        request_payload: dict[str, Any] | None = None,
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
                if isinstance(item, dict) and item.get("type") == "function_call" and "arguments" in item:
                    arguments.append(item.get("arguments"))
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
            if not isinstance(parsed, dict):
                return ContentGuardResult(
                    result=cls.RESULT_BLOCK,
                    risk_level="high",
                    categories=["tool_argument_pollution"],
                    reason="工具调用参数不是 JSON 对象",
                    action="block",
                    excerpt=cls._excerpt(dumps_json(parsed)),
                    score_delta=-20,
                )
            text = cls._extract_scan_text(parsed, max_scan_bytes=8192)
            if text:
                text_result = cls.inspect_response_text(text, request_payload=request_payload, max_scan_bytes=8192)
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
    def _payload_contains_file_reference(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = str(key).lower()
                if normalized in {"file", "files", "file_id", "file_ids", "filename", "attachments"}:
                    return True
                if normalized == "type" and isinstance(nested, str) and nested in {"input_file", "file_search"}:
                    return True
                if cls._payload_contains_file_reference(nested):
                    return True
        elif isinstance(value, list):
            return any(cls._payload_contains_file_reference(item) for item in value)
        return False

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
        return False

    @classmethod
    def _extract_scan_text(cls, value: Any, *, max_scan_bytes: int) -> str:
        parts: list[str] = []
        current_bytes = 0

        def walk(item: Any) -> None:
            nonlocal current_bytes
            if current_bytes >= max_scan_bytes:
                return
            if isinstance(item, str):
                encoded_len = len(item.encode("utf-8", errors="ignore"))
                parts.append(item)
                current_bytes += encoded_len
                return
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key in {"api_key", "authorization", "base64", "b64_json"}:
                        continue
                    walk(nested)
                    if current_bytes >= max_scan_bytes:
                        break
            elif isinstance(item, list):
                for nested in item:
                    walk(nested)
                    if current_bytes >= max_scan_bytes:
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
                for content in item.get("content") or []:
                    if not isinstance(content, dict):
                        continue
                    add(content.get("text"))
                    add(content.get("output_text"))
                    add(content.get("delta"))
                    add_scan(content.get("image_url"))
            add(value.get("output_text"))
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
        if re.search(r"(不要|禁止|不得|不能|避免|不允许|无|去除|移除).{0,16}(广告|推广|营销|落地页|优惠|链接|联系方式|外链)", text):
            return False
        if re.search(r"(广告|推广|营销|落地页|优惠|链接|联系方式|外链).{0,16}(不要|禁止|不得|不能|避免|不允许|无|去除|移除)", text):
            return False
        explicit_allow_patterns = (
            r"(生成|撰写|编写|输出|包含|加入|附带|提供|设计|创建).{0,16}(广告|推广|营销|落地页|优惠|链接|联系方式|外链)",
            r"(广告|推广|营销|落地页|优惠|链接|联系方式|外链).{0,16}(文案|内容|链接|地址|联系方式|素材)",
        )
        return any(re.search(pattern, text) for pattern in explicit_allow_patterns)
