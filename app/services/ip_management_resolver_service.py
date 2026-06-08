from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
from typing import Mapping

from fastapi import Request


@dataclass(frozen=True)
class ClientIpResolution:
    direct_client_ip: str | None
    resolved_client_ip: str | None
    resolution_source: str
    resolution_status: str
    trusted_proxy_matched: bool
    forwarded_chain: list[str] = field(default_factory=list)
    ignored_headers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "direct_client_ip": self.direct_client_ip,
            "resolved_client_ip": self.resolved_client_ip,
            "resolution_source": self.resolution_source,
            "resolution_status": self.resolution_status,
            "trusted_proxy_matched": self.trusted_proxy_matched,
            "forwarded_chain": list(self.forwarded_chain),
            "ignored_headers": list(self.ignored_headers),
            "warnings": list(self.warnings),
        }


class ClientIpResolver:
    DEFAULT_HEADER_ORDER = ["forwarded", "x_forwarded_for", "cf_connecting_ip"]

    @staticmethod
    def resolve_request(
        request: Request,
        *,
        trusted_proxy_resolution_enabled: bool,
        trusted_proxy_cidrs: list[str],
        trusted_header_order: list[str],
    ) -> ClientIpResolution:
        direct_ip = request.client.host if request.client is not None else None
        headers = {key.lower(): value for key, value in request.headers.items()}
        return ClientIpResolver.resolve(
            direct_client_ip=direct_ip,
            headers=headers,
            trusted_proxy_resolution_enabled=trusted_proxy_resolution_enabled,
            trusted_proxy_cidrs=trusted_proxy_cidrs,
            trusted_header_order=trusted_header_order,
        )

    @staticmethod
    def resolve(
        *,
        direct_client_ip: str | None,
        headers: Mapping[str, str],
        trusted_proxy_resolution_enabled: bool,
        trusted_proxy_cidrs: list[str],
        trusted_header_order: list[str],
    ) -> ClientIpResolution:
        normalized_direct = ClientIpResolver.normalize_ip(direct_client_ip)
        if not normalized_direct:
            return ClientIpResolution(
                direct_client_ip=direct_client_ip,
                resolved_client_ip=None,
                resolution_source="direct",
                resolution_status="invalid_direct_client",
                trusted_proxy_matched=False,
                warnings=["直连来源 IP 不是合法 IP"],
            )
        if not trusted_proxy_resolution_enabled:
            return ClientIpResolution(
                direct_client_ip=normalized_direct,
                resolved_client_ip=normalized_direct,
                resolution_source="direct",
                resolution_status="disabled",
                trusted_proxy_matched=False,
            )

        trusted_networks, network_warnings = ClientIpResolver.parse_networks(trusted_proxy_cidrs)
        direct_obj = ipaddress.ip_address(normalized_direct)
        trusted_proxy_matched = any(direct_obj in network for network in trusted_networks)
        if not trusted_proxy_matched:
            ignored = ClientIpResolver.present_forwarding_headers(headers)
            return ClientIpResolution(
                direct_client_ip=normalized_direct,
                resolved_client_ip=normalized_direct,
                resolution_source="direct",
                resolution_status="untrusted_header_ignored" if ignored else "direct",
                trusted_proxy_matched=False,
                ignored_headers=ignored,
                warnings=network_warnings,
            )

        header_order = trusted_header_order or ClientIpResolver.DEFAULT_HEADER_ORDER
        for header_key in header_order:
            resolution = ClientIpResolver._resolve_header(
                header_key,
                headers=headers,
                fallback_ip=normalized_direct,
                trusted_networks=trusted_networks,
                network_warnings=network_warnings,
            )
            if resolution is not None:
                return resolution
        return ClientIpResolution(
            direct_client_ip=normalized_direct,
            resolved_client_ip=normalized_direct,
            resolution_source="direct",
            resolution_status="trusted_proxy_no_valid_header",
            trusted_proxy_matched=True,
            ignored_headers=ClientIpResolver.present_forwarding_headers(headers),
            warnings=network_warnings,
        )

    @staticmethod
    def _resolve_header(
        header_key: str,
        *,
        headers: Mapping[str, str],
        fallback_ip: str,
        trusted_networks: list[ipaddress._BaseNetwork],
        network_warnings: list[str],
    ) -> ClientIpResolution | None:
        header_key = header_key.strip().lower()
        if header_key == "x_forwarded_for":
            values = ClientIpResolver.parse_x_forwarded_for(headers.get("x-forwarded-for"))
            source = "x_forwarded_for"
        elif header_key == "forwarded":
            values = ClientIpResolver.parse_forwarded(headers.get("forwarded"))
            source = "forwarded"
        elif header_key == "cf_connecting_ip":
            values = [ClientIpResolver.normalize_ip(headers.get("cf-connecting-ip"))]
            values = [item for item in values if item]
            source = "cf_connecting_ip"
        else:
            return None
        if not values:
            return None
        selected = ClientIpResolver.select_client_from_chain(values, trusted_networks)
        if selected is None:
            return ClientIpResolution(
                direct_client_ip=fallback_ip,
                resolved_client_ip=fallback_ip,
                resolution_source=source,
                resolution_status="invalid_header_ignored",
                trusted_proxy_matched=True,
                forwarded_chain=values,
                ignored_headers=[source],
                warnings=network_warnings + ["转发头中没有合法客户端 IP，已回退直连来源"],
            )
        status = "trusted_proxy"
        try:
            if all(ipaddress.ip_address(item) in network for item in values for network in trusted_networks):
                status = "all_forwarded_entries_trusted"
        except ValueError:
            pass
        return ClientIpResolution(
            direct_client_ip=fallback_ip,
            resolved_client_ip=selected,
            resolution_source=source,
            resolution_status=status,
            trusted_proxy_matched=True,
            forwarded_chain=values,
            warnings=network_warnings,
        )

    @staticmethod
    def normalize_ip(value: str | None) -> str | None:
        if value is None:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        candidate = ClientIpResolver.strip_ip_port(candidate)
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return None

    @staticmethod
    def strip_ip_port(value: str) -> str:
        candidate = value.strip().strip('"')
        if candidate.startswith("[") and "]" in candidate:
            return candidate[1:candidate.index("]")]
        if candidate.count(":") == 1 and re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", candidate):
            return candidate.rsplit(":", 1)[0]
        return candidate

    @staticmethod
    def parse_networks(values: list[str]) -> tuple[list[ipaddress._BaseNetwork], list[str]]:
        networks: list[ipaddress._BaseNetwork] = []
        warnings: list[str] = []
        for raw in values:
            text = str(raw).strip()
            if not text:
                continue
            try:
                networks.append(ipaddress.ip_network(text, strict=False))
            except ValueError:
                warnings.append(f"可信代理 CIDR 无效: {text}")
        return networks, warnings

    @staticmethod
    def parse_x_forwarded_for(value: str | None) -> list[str]:
        if not value:
            return []
        return [item for item in (ClientIpResolver.normalize_ip(part) for part in value.split(",")) if item]

    @staticmethod
    def parse_forwarded(value: str | None) -> list[str]:
        if not value:
            return []
        result: list[str] = []
        for segment in value.split(","):
            for part in segment.split(";"):
                key, sep, raw_value = part.strip().partition("=")
                if sep and key.strip().lower() == "for":
                    normalized = ClientIpResolver.normalize_ip(raw_value.strip())
                    if normalized:
                        result.append(normalized)
        return result

    @staticmethod
    def select_client_from_chain(values: list[str], trusted_networks: list[ipaddress._BaseNetwork]) -> str | None:
        valid = [item for item in values if ClientIpResolver.normalize_ip(item)]
        if not valid:
            return None
        for item in reversed(valid):
            ip_obj = ipaddress.ip_address(item)
            if not any(ip_obj in network for network in trusted_networks):
                return str(ip_obj)
        return valid[0]

    @staticmethod
    def present_forwarding_headers(headers: Mapping[str, str]) -> list[str]:
        present: list[str] = []
        if headers.get("forwarded"):
            present.append("forwarded")
        if headers.get("x-forwarded-for"):
            present.append("x_forwarded_for")
        if headers.get("cf-connecting-ip"):
            present.append("cf_connecting_ip")
        return present
