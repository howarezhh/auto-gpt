from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AlertSubscription(Base):
    __tablename__ = "alert_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_account_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    delivery_channel: Mapped[str] = mapped_column(Text, nullable=False, default="in_app")
    notify_provider_alerts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_api_key_alerts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_account_alerts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_failure_rate_alerts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    browser_notifications_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user_account = relationship("UserAccount")
