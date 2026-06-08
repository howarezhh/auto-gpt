from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import ipaddress

from app.models.ip_management import IpAccessRule


@dataclass(frozen=True)
class IpRuleMatch:
    rule: IpAccessRule | None
    action: str
    reason: str


class IpManagementRuleService:
    VALID_SCOPES = {"external_v1", "internal_api", "user_pages", "all"}
    VALID_MATCH_TYPES = {"exact_ip", "cidr", "range"}
    VALID_ACTIONS = {"allow", "record", "rate_limit", "block"}

    @staticmethod
    def normalize_rule_value(match_type: str, match_value: str) -> str:
        match_type = match_type.strip()
        value = match_value.strip()
        if match_type == "exact_ip":
            return str(ipaddress.ip_address(value))
        if match_type == "cidr":
            return str(ipaddress.ip_network(value, strict=False))
        if match_type == "range":
            start, sep, end = value.partition("-")
            if not sep:
                raise ValueError("IP 范围必须使用 起始IP-结束IP 格式")
            start_ip = ipaddress.ip_address(start.strip())
            end_ip = ipaddress.ip_address(end.strip())
            if start_ip.version != end_ip.version:
                raise ValueError("IP 范围起止地址版本必须一致")
            if int(start_ip) > int(end_ip):
                raise ValueError("IP 范围起始地址不能大于结束地址")
            return f"{start_ip}-{end_ip}"
        raise ValueError("不支持的 IP 匹配类型")

    @staticmethod
    def validate_rule_fields(*, scope: str, match_type: str, action: str) -> None:
        if scope not in IpManagementRuleService.VALID_SCOPES:
            raise ValueError("不支持的作用域")
        if match_type not in IpManagementRuleService.VALID_MATCH_TYPES:
            raise ValueError("不支持的匹配类型")
        if action not in IpManagementRuleService.VALID_ACTIONS:
            raise ValueError("不支持的处置动作")

    @staticmethod
    def match(ip_value: str | None, *, scope: str, rules: list[IpAccessRule], now: datetime | None = None) -> IpRuleMatch:
        if not ip_value:
            return IpRuleMatch(None, "allow", "no_resolved_ip")
        try:
            ip_obj = ipaddress.ip_address(ip_value)
        except ValueError:
            return IpRuleMatch(None, "allow", "invalid_resolved_ip")
        current = now or datetime.utcnow()
        candidates = [
            rule
            for rule in rules
            if rule.enabled
            and rule.scope in {scope, "all"}
            and (rule.expires_at is None or rule.expires_at > current)
        ]
        candidates.sort(key=lambda item: (item.priority, IpManagementRuleService._specificity_rank(item, ip_obj), item.id))
        for rule in candidates:
            if IpManagementRuleService._matches(rule, ip_obj):
                return IpRuleMatch(rule, rule.action, f"matched_rule:{rule.id}")
        return IpRuleMatch(None, "allow", "no_rule_matched")

    @staticmethod
    def _specificity_rank(rule: IpAccessRule, ip_obj: ipaddress._BaseAddress) -> int:
        if rule.match_type == "exact_ip":
            return -1000
        if rule.match_type == "cidr":
            try:
                network = ipaddress.ip_network(rule.normalized_value, strict=False)
                return -int(network.prefixlen)
            except ValueError:
                return 0
        return 1000

    @staticmethod
    def _matches(rule: IpAccessRule, ip_obj: ipaddress._BaseAddress) -> bool:
        try:
            if rule.match_type == "exact_ip":
                return ip_obj == ipaddress.ip_address(rule.normalized_value)
            if rule.match_type == "cidr":
                return ip_obj in ipaddress.ip_network(rule.normalized_value, strict=False)
            if rule.match_type == "range":
                start, _, end = rule.normalized_value.partition("-")
                start_ip = ipaddress.ip_address(start)
                end_ip = ipaddress.ip_address(end)
                return ip_obj.version == start_ip.version and int(start_ip) <= int(ip_obj) <= int(end_ip)
        except ValueError:
            return False
        return False
