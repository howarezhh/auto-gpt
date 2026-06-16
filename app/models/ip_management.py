from app.utils.timezone import now_beijing
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IpManagementSetting(Base):
    __tablename__ = "ip_management_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    observe_only_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trusted_proxy_resolution_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trusted_proxy_cidrs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    trusted_header_order_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    apply_external_v1_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    apply_internal_api_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    apply_user_pages_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rule_engine_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    block_action_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rate_limit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    event_logging_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    event_sample_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    event_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    store_raw_headers_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ip_masking_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fail_open_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing, onupdate=now_beijing)


class IpAccessRule(Base):
    __tablename__ = "ip_access_rules"
    __table_args__ = (
        Index("ix_ip_access_rules_enabled_scope_priority", "enabled", "scope", "priority", "id"),
        Index("ix_ip_access_rules_action_enabled", "action", "enabled"),
        Index("ix_ip_access_rules_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="external_v1")
    match_type: Mapped[str] = mapped_column(Text, nullable=False, default="cidr")
    match_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False, default="record")
    rate_qps_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rate_rpm_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing, onupdate=now_beijing)


class IpManagementEvent(Base):
    __tablename__ = "ip_management_events"
    __table_args__ = (
        Index("ix_ip_management_events_created_at", "created_at", "id"),
        Index("ix_ip_management_events_resolved_ip_created_at", "resolved_client_ip", "created_at"),
        Index("ix_ip_management_events_decision_created_at", "decision", "created_at"),
        Index("ix_ip_management_events_rule_created_at", "matched_rule_id", "created_at"),
        Index("ix_ip_management_events_trace_id", "trace_id"),
        Index("ix_ip_management_events_api_key_created_at", "api_client_key_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_log_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    direct_client_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_client_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_client_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    trusted_proxy_matched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    forwarded_chain_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    matched_rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_rule_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str] = mapped_column(Text, nullable=False, default="allow")
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_client_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_client_key_prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing)
