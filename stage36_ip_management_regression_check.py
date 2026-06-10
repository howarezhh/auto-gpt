import asyncio
import os
from pathlib import Path

TEMP_DB_PATH = Path("data/stage36-ip-management.db")
if TEMP_DB_PATH.exists():
    TEMP_DB_PATH.unlink()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test")
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["ASYNC_REQUEST_LOG_ENABLED"] = "false"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.database import Base
from app.main import app
from app.models.ip_management import IpAccessRule, IpManagementEvent, IpManagementSetting
from app.services.ip_management_event_service import IpManagementEventService
from app.services.ip_management_resolver_service import ClientIpResolution, ClientIpResolver
from app.services.ip_management_rule_service import IpManagementRuleService
from app.services.ip_management_service import IpManagementService
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


def _request(path: str = "/v1/chat/completions", client_ip: str = "198.51.100.9") -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": (client_ip, 43120),
        "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.trace_id = "stage36-trace"
    return request


def _session():
    database_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test",
    )
    engine = create_engine(database_url, future=True)
    tables = [
        Base.metadata.tables["ip_management_settings"],
        Base.metadata.tables["ip_access_rules"],
        Base.metadata.tables["ip_management_events"],
    ]
    Base.metadata.drop_all(bind=engine, tables=list(reversed(tables)))
    Base.metadata.create_all(bind=engine, tables=tables)
    return sessionmaker(bind=engine)()


def test_resolver_ignores_untrusted_forwarding_headers() -> None:
    result = ClientIpResolver.resolve(
        direct_client_ip="198.51.100.10",
        headers={"x-forwarded-for": "203.0.113.1"},
        trusted_proxy_resolution_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        trusted_header_order=["x_forwarded_for"],
    )
    assert result.resolved_client_ip == "198.51.100.10"
    assert result.resolution_status == "untrusted_header_ignored"
    assert result.ignored_headers == ["x_forwarded_for"]


def test_resolver_uses_trusted_proxy_chain_right_to_left() -> None:
    result = ClientIpResolver.resolve(
        direct_client_ip="10.0.0.2",
        headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1, 10.0.0.2"},
        trusted_proxy_resolution_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        trusted_header_order=["x_forwarded_for"],
    )
    assert result.resolved_client_ip == "203.0.113.7"
    assert result.trusted_proxy_matched is True
    assert result.resolution_source == "x_forwarded_for"


def test_forwarded_ipv6_with_port_is_parsed() -> None:
    result = ClientIpResolver.resolve(
        direct_client_ip="10.0.0.2",
        headers={"forwarded": 'for="[2001:db8::9]:443";proto=https'},
        trusted_proxy_resolution_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        trusted_header_order=["forwarded"],
    )
    assert result.resolved_client_ip == "2001:db8::9"


def test_forwarded_unbracketed_ipv6_port_is_ignored() -> None:
    result = ClientIpResolver.resolve(
        direct_client_ip="10.0.0.2",
        headers={"forwarded": "for=2001:db8::9:443;proto=https"},
        trusted_proxy_resolution_enabled=True,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        trusted_header_order=["forwarded"],
    )
    assert result.resolved_client_ip == "10.0.0.2"
    assert result.resolution_status == "trusted_proxy_no_valid_header"


def test_rule_priority_and_specificity_are_stable() -> None:
    exact = IpAccessRule(
        id=2,
        name="精确允许",
        enabled=True,
        priority=10,
        scope="external_v1",
        match_type="exact_ip",
        match_value="203.0.113.5",
        normalized_value="203.0.113.5",
        action="allow",
    )
    cidr = IpAccessRule(
        id=1,
        name="网段阻断",
        enabled=True,
        priority=10,
        scope="external_v1",
        match_type="cidr",
        match_value="203.0.113.0/24",
        normalized_value="203.0.113.0/24",
        action="block",
    )
    match = IpManagementRuleService.match("203.0.113.5", scope="external_v1", rules=[cidr, exact])
    assert match.rule is exact
    assert match.action == "allow"


def test_event_sampling_and_dedupe() -> None:
    db = _session()
    setting = IpManagementSetting(id=1, event_logging_enabled=True, event_sample_rate=0)
    resolution = ClientIpResolution("10.0.0.2", "203.0.113.7", "x_forwarded_for", "trusted_proxy", True, ["203.0.113.7"])
    assert IpManagementEventService.create_event(
        db,
        setting=setting,
        resolution=resolution,
        trace_id="trace-sampled-out",
        request_path="/v1/chat/completions",
        http_method="POST",
        scope="external_v1",
        matched_rule_id=None,
        matched_rule_name=None,
        decision="allow",
        decision_reason="no_rule_matched",
        enforced=False,
        status_code=None,
    ) is None
    setting.event_sample_rate = 100
    first = IpManagementEventService.create_event(
        db,
        setting=setting,
        resolution=resolution,
        trace_id="trace-dedupe",
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
    second = IpManagementEventService.create_event(
        db,
        setting=setting,
        resolution=resolution,
        trace_id="trace-dedupe",
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
    assert first is not None
    assert second is None
    assert db.query(IpManagementEvent).count() == 1


def test_event_dedupe_keeps_distinct_decisions() -> None:
    db = _session()
    setting = IpManagementSetting(id=1, event_logging_enabled=True, event_sample_rate=100)
    first_resolution = ClientIpResolution("10.0.0.2", "203.0.113.7", "x_forwarded_for", "trusted_proxy", True, ["203.0.113.7"])
    second_resolution = ClientIpResolution("10.0.0.2", "203.0.113.8", "x_forwarded_for", "trusted_proxy", True, ["203.0.113.8"])
    first = IpManagementEventService.create_event(
        db,
        setting=setting,
        resolution=first_resolution,
        trace_id="trace-distinct",
        request_path="/v1/chat/completions",
        http_method="POST",
        scope="external_v1",
        matched_rule_id=1,
        matched_rule_name="规则 A",
        decision="block",
        decision_reason="matched_rule:1",
        enforced=True,
        status_code=403,
    )
    second = IpManagementEventService.create_event(
        db,
        setting=setting,
        resolution=second_resolution,
        trace_id="trace-distinct",
        request_path="/v1/chat/completions",
        http_method="POST",
        scope="external_v1",
        matched_rule_id=1,
        matched_rule_name="规则 A",
        decision="block",
        decision_reason="matched_rule:1",
        enforced=True,
        status_code=403,
    )
    duplicate = IpManagementEventService.create_event(
        db,
        setting=setting,
        resolution=second_resolution,
        trace_id="trace-distinct",
        request_path="/v1/chat/completions",
        http_method="POST",
        scope="external_v1",
        matched_rule_id=1,
        matched_rule_name="规则 A",
        decision="block",
        decision_reason="matched_rule:1",
        enforced=True,
        status_code=403,
    )
    assert first is not None
    assert second is not None
    assert duplicate is None
    assert db.query(IpManagementEvent).count() == 2


def test_default_module_is_noop() -> None:
    db = _session()
    try:
        result = asyncio.run(IpManagementService.evaluate_request(db, _request()))
    finally:
        IpManagementService.invalidate_cache()
    assert result is None
    assert db.query(IpManagementEvent).count() == 0


def test_admin_page_and_api_smoke() -> None:
    with TestClient(app) as client:
        from app.database import SessionLocal

        with SessionLocal() as db:
            if UserAuthService.get_user_by_login(db, "stage36-admin") is None:
                UserAuthService.create_user(
                    db,
                    username="stage36-admin",
                    email="stage36-admin@example.com",
                    password="Stage36Admin#123",
                    role=USER_ROLE_ADMIN,
                    enabled=True,
                )
        login = client.post(
            "/login",
            data={"identifier": "stage36-admin", "password": "Stage36Admin#123"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text
        page = client.get("/ip-management")
        assert page.status_code == 200, page.text
        assert "IP 管理" in page.text
        overview = client.get("/api/ip-management/overview")
        assert overview.status_code == 200, overview.text
        assert overview.json()["settings"]["enabled"] is False
        resolution = client.post(
            "/api/ip-management/test-resolution",
            json={
                "direct_client_ip": "10.0.0.2",
                "headers": {"x-forwarded-for": "203.0.113.7, 10.0.0.2"},
                "trusted_proxy_cidrs": ["10.0.0.0/8"],
                "trusted_header_order": ["x_forwarded_for"],
                "trusted_proxy_resolution_enabled": True,
            },
        )
        assert resolution.status_code == 200, resolution.text
        assert resolution.json()["resolution"]["resolved_client_ip"] == "203.0.113.7"
        cleanup = client.post("/api/ip-management/events/cleanup")
        assert cleanup.status_code == 200, cleanup.text
        legacy_filter = client.get("/api/ip-management/events?api_key=sk-aotu")
        assert legacy_filter.status_code == 422, legacy_filter.text


def test_middleware_error_shapes_for_external_and_internal_scopes() -> None:
    from app.database import SessionLocal

    with SessionLocal() as db:
        setting = IpManagementService.get_or_create_setting(db)
        setting.enabled = True
        setting.apply_external_v1_enabled = True
        setting.apply_internal_api_enabled = True
        setting.rule_engine_enabled = True
        setting.block_action_enabled = True
        setting.event_logging_enabled = False
        db.query(IpAccessRule).delete()
        db.add(
            IpAccessRule(
                name="阻断测试来源",
                enabled=True,
                priority=1,
                scope="all",
                match_type="exact_ip",
                match_value="203.0.113.77",
                normalized_value="203.0.113.77",
                action="block",
            )
        )
        db.commit()
        IpManagementService.invalidate_cache()
    try:
        with TestClient(app, client=("203.0.113.77", 50000)) as client:
            external = client.post("/v1/chat/completions", json={"model": "gpt-4.1-mini", "messages": []})
            assert external.status_code == 403, external.text
            external_payload = external.json()
            assert "error" in external_payload
            assert external_payload["error"]["code"] == "source_ip_blocked"
            assert external_payload["error"]["trace_id"]

            internal = client.get("/api/ip-management/overview")
            assert internal.status_code == 403, internal.text
            internal_payload = internal.json()
            assert "detail" in internal_payload
            assert internal_payload["detail"]["code"] == "source_ip_blocked"
            assert "error" not in internal_payload
    finally:
        with SessionLocal() as db:
            setting = IpManagementService.get_or_create_setting(db)
            setting.enabled = False
            setting.apply_external_v1_enabled = False
            setting.apply_internal_api_enabled = False
            setting.rule_engine_enabled = False
            setting.block_action_enabled = False
            db.query(IpAccessRule).delete()
            db.commit()
            IpManagementService.invalidate_cache()


def main() -> None:
    test_resolver_ignores_untrusted_forwarding_headers()
    test_resolver_uses_trusted_proxy_chain_right_to_left()
    test_forwarded_ipv6_with_port_is_parsed()
    test_forwarded_unbracketed_ipv6_port_is_ignored()
    test_rule_priority_and_specificity_are_stable()
    test_event_sampling_and_dedupe()
    test_event_dedupe_keeps_distinct_decisions()
    test_default_module_is_noop()
    test_admin_page_and_api_smoke()
    test_middleware_error_shapes_for_external_and_internal_scopes()
    print("stage36 IP 管理模块回归检查通过")


if __name__ == "__main__":
    main()
