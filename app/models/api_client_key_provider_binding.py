from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApiClientKeyProviderBinding(Base):
    __tablename__ = "api_client_key_provider_bindings"
    __table_args__ = (
        UniqueConstraint("api_client_key_id", "provider_id", name="uq_api_client_key_provider_binding"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    api_client_key_id: Mapped[int] = mapped_column(ForeignKey("api_client_keys.id"), nullable=False, index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    api_client_key = relationship("ApiClientKey", back_populates="provider_bindings")
    provider = relationship("Provider")
