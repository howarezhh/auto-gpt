from __future__ import annotations

import json
import re
from typing import Any

from app.services.content_guard_service import ContentGuardResult, ContentGuardRule, ContentGuardService
from app.utils.json_utils import dumps_json
from app.utils.content_guard_config import validate_content_guard_url_allowlist


class ContentGuardRuleService:
    """内容防护规则与算法入口。

    ContentGuardService 负责运行时检测编排；规则注册、解析、匹配与评分统一由本服务维护。
    """

    RESULT_PASS = ContentGuardService.RESULT_PASS
    RESULT_REVIEW = ContentGuardService.RESULT_REVIEW
    RESULT_BLOCK = ContentGuardService.RESULT_BLOCK
    RESULT_ERROR = ContentGuardService.RESULT_ERROR

    _REGEX_CACHE: dict[str, re.Pattern[str]] = {}
    _RULES_JSON_CACHE: dict[str, list[ContentGuardRule]] = {}
    _MAX_REGEX_CACHE_SIZE = 512
    _MAX_RULES_JSON_CACHE_SIZE = 64
    _MAX_REGEX_PATTERN_CHARS = 500
    _MAX_REGEX_QUANTIFIER_COUNT = 24
    _MAX_AUTOMATON_CACHE_SIZE = 128
    _UNSAFE_REGEX_MARKERS = (
        r"\\[1-9]",
        "(?<=",
        "(?<!",
    )
    _AUTOMATON_CACHE: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    _LEET_TRANSLATION = str.maketrans(
        {
            "0": "o",
            "1": "i",
            "3": "e",
            "4": "a",
            "5": "s",
            "7": "t",
            "@": "a",
            "$": "s",
        }
    )
    _REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}")
    _SEPARATOR_RE = re.compile(r"[\s\W_]+", re.UNICODE)
    _ILLEGAL_TERMS = (
        "博彩",
        "赌博",
        "网赌",
        "百家乐",
        "盘口",
        "杀猪盘",
        "裸聊",
        "色情",
        "洗钱",
        "跑分",
        "套现",
        "代充",
        "诈骗",
        "黑产",
        "盗号",
        "撞库",
        "木马",
        "毒品",
        "枪支",
        "买卖身份证",
        "代开发票",
    )
    _AD_TERMS = (
        "优惠码",
        "邀请码",
        "推广链接",
        "开户链接",
        "注册送",
        "注册即送",
        "充值返现",
        "返佣",
        "送彩金",
        "赞助商",
        "折扣码",
        "扫码领取",
        "联系客服",
        "加微信",
        "telegram群",
        "whatsapp",
        "affiliate",
        "referral",
        "coupon",
        "promo",
        "sponsor",
    )
    _CTA_TERMS = (
        "开户",
        "注册",
        "充值",
        "返利",
        "返佣",
        "代理",
        "客服",
        "扫码",
        "扫描",
        "联系",
        "私聊",
        "加入",
        "加群",
        "进群",
        "领取",
        "访问",
        "点击",
        "购买",
        "下单",
        "兑换",
        "contact",
        "join",
        "register",
        "buy",
    )
    _BENIGN_REDIRECT_CONTEXT_TERMS = (
        "不要",
        "禁止",
        "不得",
        "不能",
        "避免",
        "不要点击",
        "不要添加",
        "不要加入",
        "不要扫描",
        "不建议",
        "不应",
        "未包含",
        "不包含",
        "无",
        "没有",
        "防范",
        "反诈",
        "防骗",
        "识别",
        "警惕",
        "举报",
        "屏蔽",
        "过滤",
        "检测",
        "风险",
        "安全",
    )
    _POSITIVE_REDIRECT_INTENT_TERMS = (
        "立即注册",
        "点击注册",
        "扫码领取",
        "扫描领取",
        "扫码添加",
        "添加客服",
        "联系客服",
        "加微信",
        "加入群",
        "加入社群",
        "注册送",
        "充值返现",
        "领取优惠",
        "使用优惠码",
        "访问开户链接",
        "打开开户链接",
        "点击推广链接",
        "参与返佣",
    )
    _LOW_RISK_CONTEXT_SUPPRESSIBLE_CATEGORIES = {
        "advertising_or_promotion",
        "contact_or_offplatform_redirect",
        "qr_code_redirect",
        "fraud_or_illegal_promotion",
    }

    @classmethod
    def default_rules(cls) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in ContentGuardService.DEFAULT_TEXT_RULES]

    @classmethod
    def parse_rules_json(cls, rules_json: str | None) -> list[ContentGuardRule]:
        if not rules_json:
            return cls.normalize_rules(None)
        cached = cls._RULES_JSON_CACHE.get(rules_json)
        if cached is not None:
            return list(cached)
        try:
            parsed = json.loads(rules_json)
        except Exception as exc:
            return [cls.configuration_error_rule("invalid_rules_json", "规则配置损坏", f"内容防护规则 JSON 配置损坏：{exc}")]
        normalized = cls.normalize_rules(parsed)
        if len(cls._RULES_JSON_CACHE) >= cls._MAX_RULES_JSON_CACHE_SIZE:
            cls._RULES_JSON_CACHE.clear()
        cls._RULES_JSON_CACHE[rules_json] = list(normalized)
        return normalized

    @classmethod
    def serialize_rules_json(cls, rules: list[ContentGuardRule | dict[str, Any]] | None) -> str:
        return dumps_json([rule.to_dict() for rule in cls.normalize_rules(rules)])

    @classmethod
    def normalize_rules(cls, rules: list[ContentGuardRule | dict[str, Any]] | None) -> list[ContentGuardRule]:
        use_defaults = rules is None
        source = rules if isinstance(rules, list) else list(ContentGuardService.DEFAULT_TEXT_RULES)
        normalized: list[ContentGuardRule] = []
        seen: set[str] = set()
        for index, item in enumerate(source):
            rule = item if isinstance(item, ContentGuardRule) else cls.coerce_rule(item, index=index)
            if rule is None:
                continue
            if rule.id in seen:
                normalized.append(
                    cls.configuration_error_rule(
                        f"duplicate_rule_{rule.id}_{index + 1}",
                        "规则标识重复",
                        f"内容防护规则标识重复：{rule.id}",
                    )
                )
                continue
            seen.add(rule.id)
            normalized.append(rule)
        return normalized or (list(ContentGuardService.DEFAULT_TEXT_RULES) if use_defaults else [])

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
        normalized_rules = cls.normalize_rules(rules)
        normalized_text = ContentGuardService.normalize_scan_text(text)
        lower_sample = normalized_text.lower()
        domain_scan_text = ContentGuardService.strip_markdown_code_blocks(normalized_text)
        response_domains = ContentGuardService.extract_domains(domain_scan_text)
        try:
            validated_url_allowlist = validate_content_guard_url_allowlist(url_allowlist)
        except ValueError as exc:
            return [
                cls.configuration_error_rule(
                    "invalid_url_allowlist",
                    "URL 白名单配置无效",
                    f"内容防护 URL 白名单配置无效：{exc}",
                )
            ]
        allowed_domains = ContentGuardService.allowed_domains(
            request_payload=request_payload,
            url_allowlist=validated_url_allowlist,
        )
        unexpected_domains = {domain for domain in response_domains if not ContentGuardService._domain_allowed(domain, allowed_domains)}
        matched: list[ContentGuardRule] = []
        for rule in normalized_rules:
            if not rule.enabled:
                continue
            if rule.category == "rule_configuration_error":
                matched.append(rule)
                continue
            if rule.match_type == "unexpected_url":
                if not url_check_enabled or not unexpected_domains:
                    continue
                if ContentGuardService._request_disallows_advertising_or_links(request_payload):
                    matched.append(
                        ContentGuardRule(
                            id="forbidden_unexpected_link",
                            name="禁止链接场景外链",
                            category="unexpected_link",
                            match_type="unexpected_url",
                            patterns=[],
                            risk_level="high",
                            action="block",
                            score_delta=-25,
                            confidence=0.9,
                            reason="用户明确要求不要链接、外链或联系方式，但响应包含外部链接",
                        )
                    )
                elif not ContentGuardService._request_allows_advertising(request_payload):
                    matched.append(rule)
                continue
            if rule.match_type == "keyword_any":
                terms = [str(item).strip().lower() for item in rule.patterns if str(item).strip()]
                matched_terms = [
                    term
                    for term in terms
                    if ContentGuardService._keyword_matches(lower_sample, term)
                    and not cls._is_negated_keyword_context(lower_sample, term)
                ]
                if matched_terms and not cls._is_benign_redirect_rule_context(
                    normalized_text,
                    category=rule.category,
                    terms=matched_terms,
                    has_domains=bool(response_domains),
                ):
                    matched.append(rule)
                continue
            if rule.match_type == "regex":
                for pattern in rule.patterns:
                    try:
                        compiled = cls.compile_regex_pattern(str(pattern))
                    except (re.error, ValueError) as exc:
                        matched.append(
                            cls.configuration_error_rule(
                                f"invalid_regex_{rule.id}",
                                f"无效正则：{rule.name}",
                                f"内容防护正则规则 `{rule.name}` 无效：{exc}",
                                pattern=str(pattern),
                            )
                        )
                        break
                    matched_spans = [
                        match.span()
                        for match in compiled.finditer(normalized_text)
                        if not cls._is_benign_redirect_rule_context(
                            normalized_text,
                            category=rule.category,
                            span=match.span(),
                            has_domains=bool(response_domains),
                        )
                    ]
                    if matched_spans:
                        matched.append(rule)
                        break
        if url_check_enabled and ContentGuardService._has_short_link_domain(response_domains):
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
        if (
            url_check_enabled
            and unexpected_domains
            and ContentGuardService._has_ad_intent(normalized_text)
            and not ContentGuardService._request_allows_advertising(request_payload)
        ):
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
        if enhanced_detection_enabled:
            matched.extend(
                cls.match_enhanced_text_rules(
                    normalized_text,
                    request_payload=request_payload,
                    response_domains=response_domains,
                    unexpected_domains=unexpected_domains,
                    rules=normalized_rules,
                    illegal_enabled=enhanced_illegal_enabled,
                    ad_enabled=enhanced_ad_enabled,
                    custom_enabled=enhanced_custom_enabled,
                    obfuscation_enabled=enhanced_obfuscation_enabled,
                    threshold=enhanced_threshold,
                    context_window_chars=enhanced_context_window_chars,
                )
            )
        deduped: list[ContentGuardRule] = []
        seen_rule_ids: set[str] = set()
        for rule in matched:
            if rule.id in seen_rule_ids:
                continue
            seen_rule_ids.add(rule.id)
            deduped.append(rule)
        return deduped

    @classmethod
    def match_enhanced_text_rules(
        cls,
        text: str,
        *,
        request_payload: dict[str, Any] | None,
        response_domains: set[str],
        unexpected_domains: set[str],
        rules: list[ContentGuardRule],
        illegal_enabled: bool,
        ad_enabled: bool,
        custom_enabled: bool,
        obfuscation_enabled: bool,
        threshold: int,
        context_window_chars: int,
    ) -> list[ContentGuardRule]:
        variants = cls._enhanced_text_variants(text, obfuscation_enabled=obfuscation_enabled)
        if not variants["compact"]:
            return []
        threshold = max(0, min(100, int(threshold or 70)))
        context_window_chars = max(24, min(512, int(context_window_chars or 96)))
        matched: list[ContentGuardRule] = []
        cta_terms = cls._scan_terms(variants["compact"], cls._CTA_TERMS)
        has_cta = bool(cta_terms)
        has_domain = bool(response_domains)
        request_allows_ads = ContentGuardService._request_allows_advertising(request_payload)
        request_blocks_ads = ContentGuardService._request_disallows_advertising_or_links(request_payload)

        if illegal_enabled:
            illegal_terms = cls._scan_terms(variants["compact"], cls._ILLEGAL_TERMS)
            if illegal_terms and (has_cta or has_domain or cls._terms_near(variants["compact"], illegal_terms, cta_terms, context_window_chars)):
                matched.append(
                    ContentGuardRule(
                        id="enhanced_illegal_redirect",
                        name="增强非法导流",
                        category="illegal_content",
                        match_type="keyword_any",
                        patterns=sorted(illegal_terms | cta_terms)[:20],
                        risk_level="high",
                        action="block",
                        score_delta=-30,
                        confidence=0.94,
                        reason="增强词法检测命中非法内容并伴随导流、交易或联系方式意图",
                    )
                )
            elif (
                illegal_terms
                and threshold <= 74
                and not cls._is_public_safety_education_context(variants["compact"], illegal_terms)
            ):
                matched.append(
                    ContentGuardRule(
                        id="enhanced_illegal_context_review",
                        name="增强非法内容复核",
                        category="illegal_content",
                        match_type="keyword_any",
                        patterns=sorted(illegal_terms)[:20],
                        risk_level="medium",
                        action="record",
                        score_delta=-10,
                        confidence=0.74,
                        reason="增强词法检测命中非法内容词，需要结合上下文复核",
                    )
                )

        if ad_enabled and not request_allows_ads:
            ad_terms = cls._scan_terms(variants["compact"], cls._AD_TERMS)
            ad_terms = {
                term
                for term in ad_terms
                if not cls._is_negated_compact_term(variants["compact"], term)
                and not cls._is_benign_compact_redirect_context(variants["compact"], term, has_domains=has_domain)
            }
            strong_ad_signal = bool(ad_terms and (unexpected_domains or has_cta or request_blocks_ads))
            if strong_ad_signal:
                confidence = 0.91 if unexpected_domains or request_blocks_ads else 0.82
                if int(confidence * 100) >= threshold:
                    matched.append(
                        ContentGuardRule(
                            id="enhanced_advertising_redirect",
                            name="增强广告导流",
                            category="advertising_or_promotion",
                            match_type="keyword_any",
                            patterns=sorted(ad_terms | cta_terms | unexpected_domains)[:20],
                            risk_level="high" if unexpected_domains or request_blocks_ads else "medium",
                            action="block" if unexpected_domains or request_blocks_ads else "record",
                            score_delta=-25 if unexpected_domains or request_blocks_ads else -12,
                            confidence=confidence,
                            reason="增强词法检测命中广告、优惠、返佣或站外导流意图",
                        )
                    )

        if custom_enabled:
            matched.extend(
                cls._match_enhanced_custom_keyword_rules(
                    variants["compact"],
                    rules=rules,
                    threshold=threshold,
                    has_domains=has_domain,
                )
            )
        return matched

    @classmethod
    def _match_enhanced_custom_keyword_rules(
        cls,
        compact_text: str,
        *,
        rules: list[ContentGuardRule],
        threshold: int,
        has_domains: bool,
    ) -> list[ContentGuardRule]:
        matched: list[ContentGuardRule] = []
        for rule in rules:
            if not rule.enabled or rule.action == "allow" or rule.match_type != "keyword_any" or int(rule.score_delta or 0) >= 0:
                continue
            compact_patterns = [cls._compact_detection_text(pattern) for pattern in rule.patterns if str(pattern).strip()]
            compact_patterns = [pattern for pattern in compact_patterns if len(pattern) >= 2]
            hits = cls._scan_terms(compact_text, compact_patterns)
            hits = {term for term in hits if not cls._is_negated_compact_term(compact_text, term)}
            if rule.category in cls._LOW_RISK_CONTEXT_SUPPRESSIBLE_CATEGORIES:
                hits = {
                    term
                    for term in hits
                    if not cls._is_benign_compact_redirect_context(compact_text, term, has_domains=has_domains)
                }
            if not hits:
                continue
            confidence = max(float(rule.confidence or 0.7), 0.78)
            if int(confidence * 100) < threshold:
                continue
            matched.append(
                ContentGuardRule(
                    id=f"enhanced_custom_{rule.id}",
                    name=f"增强自定义：{rule.name}",
                    category=rule.category,
                    match_type="keyword_any",
                    patterns=sorted(hits)[:20],
                    risk_level=rule.risk_level,
                    action=rule.action,
                    score_delta=rule.score_delta,
                    confidence=confidence,
                    reason=rule.reason or "增强词法检测命中自定义禁止内容",
                )
            )
        return matched

    @classmethod
    def _enhanced_text_variants(cls, text: str, *, obfuscation_enabled: bool) -> dict[str, str]:
        normalized = ContentGuardService.normalize_scan_text(text).lower()
        folded = normalized.translate(cls._LEET_TRANSLATION) if obfuscation_enabled else normalized
        folded = cls._REPEATED_CHAR_RE.sub(r"\1\1", folded)
        compact = cls._compact_detection_text(folded if obfuscation_enabled else normalized)
        return {"normalized": normalized, "folded": folded, "compact": compact}

    @classmethod
    def _compact_detection_text(cls, text: str) -> str:
        normalized = ContentGuardService.normalize_scan_text(str(text or "")).lower()
        normalized = normalized.translate(cls._LEET_TRANSLATION)
        normalized = cls._REPEATED_CHAR_RE.sub(r"\1\1", normalized)
        return cls._SEPARATOR_RE.sub("", normalized)

    @classmethod
    def _scan_terms(cls, text: str, terms: tuple[str, ...] | list[str] | set[str]) -> set[str]:
        compact_terms = tuple(
            sorted(
                {
                    cls._compact_detection_text(term)
                    for term in terms
                    if cls._compact_detection_text(term)
                }
            )
        )
        if not text or not compact_terms:
            return set()
        automaton = cls._get_automaton(compact_terms)
        state = 0
        hits: set[str] = set()
        for char in text:
            while state and char not in automaton[state]["next"]:
                state = automaton[state]["fail"]
            state = automaton[state]["next"].get(char, 0)
            for term in automaton[state]["out"]:
                hits.add(term)
        return hits

    @classmethod
    def _get_automaton(cls, terms: tuple[str, ...]) -> list[dict[str, Any]]:
        cached = cls._AUTOMATON_CACHE.get(terms)
        if cached is not None:
            return cached
        nodes: list[dict[str, Any]] = [{"next": {}, "fail": 0, "out": []}]
        for term in terms:
            state = 0
            for char in term:
                next_map = nodes[state]["next"]
                if char not in next_map:
                    next_map[char] = len(nodes)
                    nodes.append({"next": {}, "fail": 0, "out": []})
                state = next_map[char]
            nodes[state]["out"].append(term)
        queue: list[int] = []
        for next_state in nodes[0]["next"].values():
            queue.append(next_state)
        for state in queue:
            for char, next_state in nodes[state]["next"].items():
                fail_state = nodes[state]["fail"]
                while fail_state and char not in nodes[fail_state]["next"]:
                    fail_state = nodes[fail_state]["fail"]
                nodes[next_state]["fail"] = nodes[fail_state]["next"].get(char, 0)
                nodes[next_state]["out"].extend(nodes[nodes[next_state]["fail"]]["out"])
                queue.append(next_state)
        if len(cls._AUTOMATON_CACHE) >= cls._MAX_AUTOMATON_CACHE_SIZE:
            cls._AUTOMATON_CACHE.clear()
        cls._AUTOMATON_CACHE[terms] = nodes
        return nodes

    @staticmethod
    def _terms_near(text: str, left_terms: set[str], right_terms: set[str], window_chars: int) -> bool:
        if not left_terms or not right_terms:
            return False
        left_positions = [
            text.find(term)
            for term in left_terms
            if term and text.find(term) >= 0
        ]
        right_positions = [
            text.find(term)
            for term in right_terms
            if term and text.find(term) >= 0
        ]
        return any(abs(left - right) <= window_chars for left in left_positions for right in right_positions)

    @staticmethod
    def _is_negated_keyword_context(sample: str, term: str) -> bool:
        if not sample or not term:
            return False
        index = sample.find(term)
        if index < 0:
            return False
        prefix = sample[max(0, index - 12):index]
        return bool(re.search(r"(没有|無|无|不含|不包含|未包含|未提供|不提供|不会包含|不会提供|不存在|无任何)\s*$", prefix))

    @classmethod
    def _is_benign_redirect_rule_context(
        cls,
        normalized_text: str,
        *,
        category: str,
        terms: list[str] | None = None,
        span: tuple[int, int] | None = None,
        has_domains: bool,
    ) -> bool:
        if category not in cls._LOW_RISK_CONTEXT_SUPPRESSIBLE_CATEGORIES:
            return False
        if has_domains:
            return False
        if span is not None:
            start, end = span
        else:
            positions = [
                (index, index + len(str(term)))
                for term in terms or []
                for index in [normalized_text.lower().find(str(term).lower())]
                if index >= 0
            ]
            if not positions:
                return False
            start = min(item[0] for item in positions)
            end = max(item[1] for item in positions)
        window = normalized_text[max(0, start - 36): min(len(normalized_text), end + 48)]
        return cls._is_benign_compact_redirect_context(
            cls._compact_detection_text(window),
            None,
            has_domains=False,
        )

    @classmethod
    def _is_benign_compact_redirect_context(
        cls,
        compact_text: str,
        compact_term: str | None,
        *,
        has_domains: bool,
    ) -> bool:
        if has_domains or not compact_text:
            return False
        if compact_term:
            index = compact_text.find(compact_term)
            if index < 0:
                return False
            window = compact_text[max(0, index - 18): index + len(compact_term) + 24]
        else:
            window = compact_text
        safety_terms = tuple(cls._compact_detection_text(item) for item in cls._BENIGN_REDIRECT_CONTEXT_TERMS)
        has_safety_context = any(term and term in window for term in safety_terms)
        if not has_safety_context:
            return False
        positive_terms = tuple(cls._compact_detection_text(item) for item in cls._POSITIVE_REDIRECT_INTENT_TERMS)
        for term in positive_terms:
            if not term or term not in window:
                continue
            if not cls._is_negated_compact_term(window, term):
                return False
        return True

    @classmethod
    def _is_negated_compact_term(cls, compact_text: str, compact_term: str) -> bool:
        if not compact_text or not compact_term:
            return False
        index = compact_text.find(compact_term)
        if index < 0:
            return False
        compact_negations = tuple(
            cls._compact_detection_text(item)
            for item in (
                "没有",
                "无",
                "不要",
                "禁止",
                "不得",
                "不能",
                "避免",
                "不建议",
                "不应",
                "不含",
                "不包含",
                "未包含",
                "未提供",
                "不提供",
                "不会包含",
                "不会提供",
                "不存在",
                "无任何",
            )
        )
        prefix = compact_text[max(0, index - 10):index]
        short_prefix = prefix[-6:]
        return any(
            prefix.endswith(item) or item in short_prefix
            for item in compact_negations
            if item
        )

    @classmethod
    def _is_public_safety_education_context(cls, compact_text: str, illegal_terms: set[str]) -> bool:
        if not compact_text or not illegal_terms:
            return False
        safety_terms = tuple(
            cls._compact_detection_text(item)
            for item in (
                "防范",
                "反诈",
                "防骗",
                "识别",
                "警惕",
                "举报",
                "避免",
                "拒绝",
                "核验来源",
                "保留证据",
                "安全提示",
            )
        )
        for illegal_term in illegal_terms:
            index = compact_text.find(illegal_term)
            if index < 0:
                continue
            window = compact_text[max(0, index - 12): index + len(illegal_term) + 18]
            if any(term and term in window for term in safety_terms):
                return True
        return False

    inspect_response_text = staticmethod(ContentGuardService.inspect_response_text)
    inspect_json_response = staticmethod(ContentGuardService.inspect_json_response)
    inspect_sse_event = staticmethod(ContentGuardService.inspect_sse_event)
    extract_response_scan_text = staticmethod(ContentGuardService._extract_response_scan_text)
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

    @classmethod
    def coerce_rule(cls, item: Any, *, index: int) -> ContentGuardRule | None:
        if not isinstance(item, dict):
            return cls.configuration_error_rule(
                f"invalid_rule_{index + 1}",
                "规则结构无效",
                f"第 {index + 1} 条内容防护规则必须是 JSON 对象",
            )
        raw_id = str(item.get("id") or f"custom_rule_{index + 1}")
        rule_id = cls.normalize_rule_id(raw_id)
        name = str(item.get("name") or rule_id).strip()[:80]
        category = cls.normalize_rule_id(str(item.get("category") or rule_id))
        patterns = item.get("patterns")
        if isinstance(patterns, str):
            patterns = [line.strip() for line in patterns.splitlines() if line.strip()]
        if not isinstance(patterns, list):
            patterns = []
        clean_patterns = [str(pattern).strip()[:500] for pattern in patterns if str(pattern).strip()]
        if not rule_id or not name or not category or not clean_patterns:
            return cls.configuration_error_rule(
                f"invalid_rule_{index + 1}",
                "规则字段缺失",
                f"第 {index + 1} 条内容防护规则缺少标识、名称、分类或匹配项",
            )
        match_type = str(item.get("match_type") or "keyword_any").strip()
        risk_level = str(item.get("risk_level") or "medium").strip()
        action = str(item.get("action") or "record").strip()
        invalid_fields: list[str] = []
        if match_type not in ContentGuardService._ALLOWED_RULE_MATCH_TYPES:
            invalid_fields.append(f"match_type={match_type}")
        if risk_level not in ContentGuardService._ALLOWED_RULE_RISK_LEVELS:
            invalid_fields.append(f"risk_level={risk_level}")
        if action not in ContentGuardService._ALLOWED_RULE_ACTIONS:
            invalid_fields.append(f"action={action}")
        if invalid_fields:
            return cls.configuration_error_rule(
                f"invalid_rule_{rule_id}",
                "规则枚举无效",
                f"内容防护规则 `{rule_id}` 存在无效枚举：{', '.join(invalid_fields)}",
            )
        try:
            score_delta = int(item.get("score_delta", -8))
        except Exception:
            return cls.configuration_error_rule(
                f"invalid_rule_{rule_id}",
                "规则扣分无效",
                f"内容防护规则 `{rule_id}` 的 score_delta 必须是整数",
            )
        try:
            confidence = float(item.get("confidence", 0.7))
        except Exception:
            return cls.configuration_error_rule(
                f"invalid_rule_{rule_id}",
                "规则置信度无效",
                f"内容防护规则 `{rule_id}` 的 confidence 必须是数字",
            )
        if action == "allow" and score_delta != 0:
            return cls.configuration_error_rule(
                f"invalid_rule_{rule_id}",
                "放行规则扣分无效",
                f"内容防护规则 `{rule_id}` 的放行动作必须使用 score_delta=0",
            )
        if action == "block" and risk_level != "high":
            return cls.configuration_error_rule(
                f"invalid_rule_{rule_id}",
                "阻断规则风险等级无效",
                f"内容防护规则 `{rule_id}` 的阻断动作必须使用 high 风险等级",
            )
        if match_type == "regex":
            for pattern in clean_patterns:
                try:
                    cls.compile_regex_pattern(pattern)
                except (re.error, ValueError) as exc:
                    return cls.configuration_error_rule(
                        f"invalid_regex_{rule_id}",
                        f"无效正则：{name}",
                        f"内容防护正则规则 `{name}` 无效：{exc}",
                        pattern=pattern,
                    )
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
            score_delta=max(-100, min(100, score_delta)),
            confidence=confidence,
            reason=str(item.get("reason") or "").strip()[:200],
        )

    @staticmethod
    def with_latency(result: ContentGuardResult, *, started) -> ContentGuardResult:
        return ContentGuardService._with_latency(result, started=started)

    @classmethod
    def configuration_error_rule(
        cls,
        rule_id: str,
        name: str,
        reason: str,
        *,
        pattern: str | None = None,
    ) -> ContentGuardRule:
        return ContentGuardRule(
            id=cls.normalize_rule_id(rule_id) or "invalid_rule",
            name=name[:80],
            category="rule_configuration_error",
            match_type="keyword_any",
            patterns=[pattern or "__invalid_content_guard_rule__"],
            risk_level="high",
            action="record",
            score_delta=0,
            confidence=1.0,
            reason=reason[:200],
        )

    @classmethod
    def compile_regex_pattern(cls, pattern: str) -> re.Pattern[str]:
        raw = str(pattern or "")
        cls.validate_regex_pattern(raw)
        cached = cls._REGEX_CACHE.get(raw)
        if cached is not None:
            return cached
        compiled = re.compile(raw, re.IGNORECASE)
        if len(cls._REGEX_CACHE) >= cls._MAX_REGEX_CACHE_SIZE:
            cls._REGEX_CACHE.clear()
        cls._REGEX_CACHE[raw] = compiled
        return compiled

    @classmethod
    def validate_regex_pattern(cls, pattern: str) -> None:
        if not pattern:
            raise ValueError("正则不能为空")
        if len(pattern) > cls._MAX_REGEX_PATTERN_CHARS:
            raise ValueError(f"正则长度不能超过 {cls._MAX_REGEX_PATTERN_CHARS} 个字符")
        if sum(1 for char in pattern if char in "*+{") > cls._MAX_REGEX_QUANTIFIER_COUNT:
            raise ValueError("正则量词过多，存在热路径性能风险")
        if any(marker in pattern for marker in cls._UNSAFE_REGEX_MARKERS):
            raise ValueError("正则包含反向引用或后行断言，存在热路径性能风险")
        if re.search(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{)", pattern):
            raise ValueError("正则包含嵌套量词，存在灾难性回溯风险")

    @staticmethod
    def split_allow_rules(rules: list[ContentGuardRule]) -> tuple[list[ContentGuardRule], list[ContentGuardRule]]:
        allow_rules = [rule for rule in rules if rule.action == "allow"]
        risk_rules = [rule for rule in rules if rule.action != "allow"]
        return allow_rules, risk_rules

    @staticmethod
    def score_rules(rules: list[ContentGuardRule]) -> tuple[int, int]:
        score_delta = sum(int(rule.score_delta or 0) for rule in rules)
        score_delta = max(-100, min(100, score_delta))
        risk_points = max(0, -score_delta)
        return score_delta, risk_points

    @classmethod
    def aggregate_probe_guard_result(cls, results: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [
            item.get("content_guard")
            for item in results
            if isinstance(item.get("content_guard"), dict)
        ]
        if not candidates:
            return None
        priority = {
            cls.RESULT_BLOCK: 4,
            cls.RESULT_ERROR: 3,
            cls.RESULT_REVIEW: 2,
            cls.RESULT_PASS: 1,
        }
        return max(candidates, key=lambda item: priority.get(str(item.get("content_guard_result") or ""), 0))

    @classmethod
    def summarize_probe_guard_results(
        cls,
        results: list[dict[str, Any]],
        *,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        total = len(results)
        passed = sum(1 for item in results if item.get("success") is True)
        content_results = [
            str((item.get("content_guard") or {}).get("content_guard_result") or "")
            for item in results
            if isinstance(item.get("content_guard"), dict)
        ]
        result = str(decision.get("content_guard_result") or "")
        if result == cls.RESULT_BLOCK or cls.RESULT_BLOCK in content_results:
            status = "blocked"
            result = cls.RESULT_BLOCK
        elif result in {cls.RESULT_REVIEW, cls.RESULT_ERROR} or cls.RESULT_REVIEW in content_results or cls.RESULT_ERROR in content_results or passed < total:
            status = "review"
            result = cls.RESULT_REVIEW
        else:
            status = "passed"
            result = cls.RESULT_PASS
        return {
            "status": status,
            "content_guard_result": result,
            "content_guard_reason": decision.get("content_guard_reason"),
            "total": total,
            "passed": passed,
            "failed": max(0, total - passed),
        }
