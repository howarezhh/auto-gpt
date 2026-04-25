from datetime import datetime

from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserAccount(Base):
    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user_accounts.id"), nullable=True)
    frozen_amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    request_limit_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_limit_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_limit_monthly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_limit_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_limit_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_limit_monthly: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_limit_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cost_limit_daily: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cost_limit_monthly: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    created_by_user = relationship("UserAccount", remote_side=[id], backref="created_users")
    owned_api_keys = relationship("ApiClientKey", back_populates="owner_user")
