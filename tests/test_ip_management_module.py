from __future__ import annotations

from types import SimpleNamespace

from app.models.ip_management import IpManagementEvent
from app.services.ip_management_event_service import IpManagementEventService
from app.services.ip_management_resolver_service import ClientIpResolver
from app.services.ip_management_rule_service import IpManagementRuleService
from app.services.proxy_request_context import (
    clear_current_ip_management_event_id,
    get_current_ip_management_event_id,
)
from app.services.request_log_queue_service import RequestLogQueueService


class FakeIpEventSession:
    def __init__(self) -> None:
        self._items: dict[int, IpManagementEvent] = {}
        self._next_id = 1

    def add(self, item) -> None:
        if isinstance(item, IpManagementEvent):
            if item.id is None:
                item.id = self._next_id
                self._next_id += 1
            self._items[item.id] = item

    def commit(self) -> None:
        return None

    def refresh(self, _item) -> None:
        return None

    def flush(self) -> None:
        return None

    def get(self, model, item_id):
        if model is not IpManagementEvent:
            return None
        return self._items.get(int(item_id))

    def scalar(self, *_args, **_kwargs):
        return None


def test_untrusted_forwarding_headers_are_ignored() -> None:
    resolution = ClientIpResolver.resolve(
        direct_client_ip="198.51.100.10",
        headers={
            "x-forwarded-for": "203.0.113.9",
            "forwarded": "for=203.0.113.8;proto=https",
            "cf-connecting-ip": "203.0.113.7",
        },
        trusted_proxy_resolution_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        trusted_header_order=["forwarded", "x_forwarded_for", "cf_connecting_ip"],
    )

    assert resolution.resolved_client_ip == "198.51.100.10"
    assert resolution.resolution_status == "untrusted_header_ignored"
    assert resolution.trusted_proxy_matched is False
    assert set(resolution.ignored_headers) == {"forwarded", "x_forwarded_for", "cf_connecting_ip"}


def test_trusted_proxy_resolution_keeps_chain_audit() -> None:
    resolution = ClientIpResolver.resolve(
        direct_client_ip="10.0.0.5",
        headers={"x-forwarded-for": "203.0.113.9, 10.0.0.6"},
        trusted_proxy_resolution_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        trusted_header_order=["x_forwarded_for"],
    )

    assert resolution.resolved_client_ip == "203.0.113.9"
    assert resolution.resolution_source == "x_forwarded_for"
    assert resolution.resolution_status == "trusted_proxy"
    assert resolution.trusted_proxy_matched is True
    assert resolution.forwarded_chain == ["203.0.113.9", "10.0.0.6"]


def test_external_native_protocol_paths_map_to_external_scope() -> None:
    from app.services.ip_management_service import IpManagementService

    assert IpManagementService.resolve_scope("/v1beta/models/gemini-2.5-pro:generateContent") == "external_v1"
    assert IpManagementService.resolve_scope("/v1beta/models/gemini-2.5-pro:streamGenerateContent") == "external_v1"
    assert IpManagementService.resolve_scope("/v1/messages") == "external_v1"


def test_rule_priority_prefers_more_specific_rule_with_same_priority() -> None:
    rules = [
        SimpleNamespace(
            id=1,
            enabled=True,
            priority=10,
            scope="external_v1",
            match_type="cidr",
            normalized_value="203.0.113.0/24",
            action="block",
            expires_at=None,
        ),
        SimpleNamespace(
            id=2,
            enabled=True,
            priority=10,
            scope="external_v1",
            match_type="exact_ip",
            normalized_value="203.0.113.9",
            action="allow",
            expires_at=None,
        ),
    ]

    match = IpManagementRuleService.match("203.0.113.9", scope="external_v1", rules=rules)

    assert match.rule.id == 2
    assert match.action == "allow"


def test_event_creation_sets_current_event_context() -> None:
    fake_db_session = FakeIpEventSession()
    clear_current_ip_management_event_id()
    setting = SimpleNamespace(
        event_logging_enabled=True,
        event_sample_rate=100,
        ip_masking_enabled=False,
        store_raw_headers_enabled=True,
    )
    resolution = ClientIpResolver.resolve(
        direct_client_ip="10.0.0.5",
        headers={"x-forwarded-for": "203.0.113.9"},
        trusted_proxy_resolution_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        trusted_header_order=["x_forwarded_for"],
    )

    event = IpManagementEventService.create_event(
        fake_db_session,
        setting=setting,
        resolution=resolution,
        trace_id="trace-ip-1",
        request_path="/v1/chat/completions",
        http_method="POST",
        scope="external_v1",
        matched_rule_id=None,
        matched_rule_name=None,
        decision="allow",
        decision_reason="no_rule_matched",
        enforced=False,
        status_code=None,
    )

    assert event is not None
    assert get_current_ip_management_event_id() == event.id


def test_attach_request_context_fills_audit_fields() -> None:
    fake_db_session = FakeIpEventSession()
    event = IpManagementEvent(
        trace_id="trace-ip-2",
        request_path="/v1/chat/completions",
        http_method="POST",
        scope="external_v1",
        decision="allow",
        enforced=False,
    )
    fake_db_session.add(event)
    fake_db_session.commit()
    fake_db_session.refresh(event)

    attached = IpManagementEventService.attach_request_context(
        fake_db_session,
        event_id=event.id,
        request_log_id=321,
        api_client_key_id=12,
        api_client_key_prefix="sk-aotu-test",
        user_account_id=34,
    )

    assert attached is True
    assert event.request_log_id == 321
    assert event.api_client_key_id == 12
    assert event.api_client_key_prefix == "sk-aotu-test"
    assert event.user_account_id == 34


def test_request_log_queue_carries_current_ip_event_id(monkeypatch) -> None:
    captured: dict = {}

    clear_current_ip_management_event_id()
    monkeypatch.setattr("app.services.request_log_queue_service.RequestLogQueueService.enabled", lambda: True)
    monkeypatch.setattr("app.services.request_log_queue_service.get_current_ip_management_event_id", lambda: 456)
    monkeypatch.setattr("app.services.request_log_queue_service.RedisService.event_loop", lambda: None)

    class FakeClient:
        def lpush(self, _key, raw_item):
            captured["raw_item"] = raw_item
            return 1

    monkeypatch.setattr("app.services.request_log_queue_service.RedisService.get_sync_client", lambda: FakeClient())

    queued = RequestLogQueueService.enqueue(log_type="chat", success=True, trace_id="trace-ip-3")

    assert queued is True
    assert '"ip_management_event_id": 456' in captured["raw_item"]


def test_ip_event_list_supports_request_log_filters() -> None:
    source = __import__("pathlib").Path("app/services/ip_management_event_service.py").read_text(encoding="utf-8")
    router_source = __import__("pathlib").Path("app/routers/ip_management.py").read_text(encoding="utf-8")

    assert "api_key: str | None = None" in source
    assert "request_log_id: int | None = None" in source
    assert "user_account_id: int | None = None" in source
    assert "IpManagementEvent.api_client_key_prefix" in source
    assert "IpManagementEvent.request_log_id == request_log_id" in source
    assert "IpManagementEvent.user_account_id == user_account_id" in source
    assert "api_key: str | None = None" in router_source
    assert "request_log_id: int | None = None" in router_source
    assert "user_account_id: int | None = None" in router_source
    assert "api_key=api_key" in router_source
    assert "request_log_id=request_log_id" in router_source
    assert "user_account_id=user_account_id" in router_source


def test_trace_runtime_middleware_preserves_ip_event_context() -> None:
    source = __import__("pathlib").Path("app/main.py").read_text(encoding="utf-8")

    assert 'if not getattr(request.state, "ip_management", None):' in source
    assert "clear_current_ip_management_event_id()" in source
