from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ModelMapping(Base):
    __tablename__ = "model_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_model_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    targets_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
