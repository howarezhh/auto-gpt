from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    provider_type: Mapped[str] = mapped_column(Text, nullable=False, default="openai_compatible")
    group_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    region_tag: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=30000)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_active_requests: Mapped[int | None] = mapped_column(Integer, nullable=True, default=300)
    max_active_streams: Mapped[int | None] = mapped_column(Integer, nullable=True, default=150)
    max_qps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_error_rate: Mapped[float | None] = mapped_column(Float, nullable=True, default=80.0)
    first_token_timeout_sec: Mapped[int | None] = mapped_column(Integer, nullable=True, default=60)
    maintenance_window: Mapped[str | None] = mapped_column(Text, nullable=True)
    maintenance_mode_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_circuit_break_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_recover_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    circuit_breaker_threshold_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recovery_probe_interval_sec_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    models_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    health_status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    circuit_state: Mapped[str] = mapped_column(Text, nullable=False, default="closed")
    credential_rotated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    credential_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    provider_models = relationship(
        "ProviderModel",
        back_populates="provider",
        cascade="all, delete-orphan",
        order_by="ProviderModel.priority.asc(), ProviderModel.id.asc()",
    )
