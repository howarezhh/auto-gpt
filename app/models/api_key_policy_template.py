from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ApiKeyPolicyTemplate(Base):
    __tablename__ = "api_key_policy_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    route_mode: Mapped[str] = mapped_column(Text, nullable=False, default="failover")
    default_provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_allow_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    token_limit_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_limit_total: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    expires_in_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allowed_provider_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_model_names_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
