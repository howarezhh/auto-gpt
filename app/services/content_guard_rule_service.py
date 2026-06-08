from __future__ import annotations

from typing import Any

from app.services.content_guard_service import ContentGuardResult, ContentGuardRule, ContentGuardService


class ContentGuardRuleService:
    """内容防护规则与算法入口。

    旧的 ContentGuardService 仍保留底层确定性实现，所有模块外调用必须经过本服务暴露的规则接口。
    """

    RESULT_PASS = ContentGuardService.RESULT_PASS
    RESULT_REVIEW = ContentGuardService.RESULT_REVIEW
    RESULT_BLOCK = ContentGuardService.RESULT_BLOCK
    RESULT_ERROR = ContentGuardService.RESULT_ERROR

    default_rules = staticmethod(ContentGuardService.default_rules)
    parse_rules_json = staticmethod(ContentGuardService.parse_rules_json)
    serialize_rules_json = staticmethod(ContentGuardService.serialize_rules_json)
    normalize_rules = staticmethod(ContentGuardService.normalize_rules)
    match_text_rules = staticmethod(ContentGuardService.match_text_rules)
    inspect_response_text = staticmethod(ContentGuardService.inspect_response_text)
    inspect_json_response = staticmethod(ContentGuardService.inspect_json_response)
    inspect_sse_event = staticmethod(ContentGuardService.inspect_sse_event)
    requires_trusted_provider = staticmethod(ContentGuardService.requires_trusted_provider)
    should_block = staticmethod(ContentGuardService.should_block)
    record_violation = staticmethod(ContentGuardService.record_violation)
    normalize_scan_text = staticmethod(ContentGuardService.normalize_scan_text)
    parse_url_allowlist = staticmethod(ContentGuardService.parse_url_allowlist)
    allowed_domains = staticmethod(ContentGuardService.allowed_domains)
    extract_domains = staticmethod(ContentGuardService.extract_domains)

    @staticmethod
    def normalize_rule_id(value: str) -> str:
        return ContentGuardService._normalize_rule_id(value)

    @staticmethod
    def summarize_matched_rules(rules: list[ContentGuardRule]) -> str:
        return ContentGuardService._summarize_matched_rules(rules)

    @staticmethod
    def clip_text(text: str | None, *, max_scan_bytes: int) -> str:
        return ContentGuardService._clip_text(text, max_scan_bytes=max_scan_bytes)

    @staticmethod
    def excerpt(text: str | None) -> str | None:
        return ContentGuardService._excerpt(text)

    @staticmethod
    def coerce_rule(item: Any, *, index: int) -> ContentGuardRule | None:
        return ContentGuardService._coerce_rule(item, index=index)

    @staticmethod
    def with_latency(result: ContentGuardResult, *, started) -> ContentGuardResult:
        return ContentGuardService._with_latency(result, started=started)
