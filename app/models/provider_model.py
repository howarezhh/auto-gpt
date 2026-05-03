from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.decimal_utils import (
    DB_MULTIPLIER_PRECISION,
    DB_MULTIPLIER_SCALE,
    DB_PRICE_PRECISION,
    DB_PRICE_SCALE,
)


class ProviderModel(Base):
    __tablename__ = "provider_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    model_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    health_status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    circuit_state: Mapped[str] = mapped_column(Text, nullable=False, default="closed")
    circuit_opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    supports_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_vision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_chat_completions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_responses: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_multiplier: Mapped[Decimal] = mapped_column(Numeric(DB_MULTIPLIER_PRECISION, DB_MULTIPLIER_SCALE), nullable=False, default=Decimal("1"))
    input_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    output_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    cache_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    provider = relationship("Provider", back_populates="provider_models")
