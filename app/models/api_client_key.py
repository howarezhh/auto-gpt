from datetime import datetime

from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.decimal_utils import DB_MONEY_PRECISION, DB_MONEY_SCALE


class ApiClientKey(Base):
    __tablename__ = "api_client_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    raw_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    token_limit_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_limit_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_limit_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_limit_daily: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    qps_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_limit_total: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    total_cost_used: Mapped[Decimal] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=False, default=Decimal("0"))
    balance_amount: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    total_recharge_amount: Mapped[Decimal] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=False, default=Decimal("0"))
    route_mode: Mapped[str] = mapped_column(Text, nullable=False, default="failover")
    default_provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"), nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("user_accounts.id"), nullable=True, index=True)
    manual_allow_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allowed_model_names_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_endpoint_paths_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_source_ips_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    preferred_provider_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    preferred_region_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    max_candidate_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_bias: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    success_rate_bias: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cost_bias: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    provider_bindings = relationship(
        "ApiClientKeyProviderBinding",
        back_populates="api_client_key",
        cascade="all, delete-orphan",
    )
    owner_user = relationship("UserAccount", back_populates="owned_api_keys")
