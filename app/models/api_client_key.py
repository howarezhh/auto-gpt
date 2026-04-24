from datetime import datetime

from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApiClientKey(Base):
    __tablename__ = "api_client_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    token_limit_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_limit_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    total_cost_used: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    balance_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    total_recharge_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    route_mode: Mapped[str] = mapped_column(Text, nullable=False, default="failover")
    default_provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"), nullable=True)
    manual_allow_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
