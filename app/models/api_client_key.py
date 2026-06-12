from app.utils.timezone import now_beijing
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
    qps_limit: Mapped[int | None] = mapped_column(Integer, nullable=True, default=20)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True, default=20)
    prompt_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_used: Mapped[Decimal] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=False, default=Decimal("0"))
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), nullable=False, index=True)
    auto_sync_provider_bindings: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allowed_model_names_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_endpoint_paths_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_source_ips_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    preferred_provider_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    preferred_region_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    latency_bias: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    success_rate_bias: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=now_beijing)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=now_beijing,
        onupdate=now_beijing,
    )

    provider_bindings = relationship(
        "ApiClientKeyProviderBinding",
        back_populates="api_client_key",
        cascade="all, delete-orphan",
    )
    owner_user = relationship("UserAccount", back_populates="owned_api_keys")
