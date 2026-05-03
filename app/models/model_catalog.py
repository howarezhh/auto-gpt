from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.utils.decimal_utils import DB_PRICE_PRECISION, DB_PRICE_SCALE


class ModelCatalog(Base):
    __tablename__ = "model_catalogs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    model_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_vision: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_chat_completions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_responses: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    output_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    cache_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(DB_PRICE_PRECISION, DB_PRICE_SCALE), nullable=True)
    speed_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
