from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.decimal_utils import (
    DB_MONEY_PRECISION,
    DB_MONEY_SCALE,
    DB_PRICE_PRECISION,
    DB_PRICE_SCALE,
)


class UserAccountBillingRecord(Base):
    __tablename__ = "user_account_billing_records"
    __table_args__ = (
        UniqueConstraint("request_log_id", name="uq_user_account_billing_records_request_log_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_account_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    api_client_key_id: Mapped[int | None] = mapped_column(ForeignKey("api_client_keys.id", ondelete="SET NULL"), nullable=True, index=True)
    request_log_id: Mapped[int | None] = mapped_column(ForeignKey("request_logs.id", ondelete="SET NULL"), nullable=True, index=True)
    record_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=False, default=Decimal("0"))
    balance_after: Mapped[Decimal | None] = mapped_column(Numeric(DB_MONEY_PRECISION, DB_MONEY_SCALE), nullable=True)
    provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_input_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    unit_output_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    user_account = relationship("UserAccount")
    api_client_key = relationship("ApiClientKey")
    request_log = relationship("RequestLog")
