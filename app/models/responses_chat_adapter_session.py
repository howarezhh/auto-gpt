from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ResponsesChatAdapterSession(Base):
    """存储 Responses→Chat 兼容适配层的 response_id 会话快照。"""

    __tablename__ = "responses_chat_adapter_sessions"
    __table_args__ = (
        Index("ix_responses_chat_adapter_sessions_expires_at", "expires_at"),
    )

    response_id: Mapped[str] = mapped_column(Text, primary_key=True)
    previous_response_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    requested_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    messages_json: Mapped[str] = mapped_column(Text, nullable=False)
    pending_tool_call_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_round_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
