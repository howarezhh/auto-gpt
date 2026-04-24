from datetime import datetime

from pydantic import BaseModel


class ConversationSummaryItem(BaseModel):
    conversation_key: str
    request_count: int
    success_count: int
    failure_count: int
    total_tokens: int
    started_at: datetime
    updated_at: datetime
    latest_model: str | None = None
    latest_provider_name: str | None = None
    preview_text: str | None = None


class ConversationSummaryList(BaseModel):
    total: int
    items: list[ConversationSummaryItem]


class ConversationTurn(BaseModel):
    role: str
    content: str
    created_at: datetime
    request_id: str | None = None
    requested_model: str | None = None
    provider_name: str | None = None
    total_tokens: int | None = None
    log_id: int | None = None
    is_stream: bool = False
    has_image: bool = False


class ConversationReplay(BaseModel):
    conversation_key: str
    request_count: int
    success_count: int
    failure_count: int
    total_tokens: int
    started_at: datetime
    updated_at: datetime
    latest_model: str | None = None
    latest_provider_name: str | None = None
    turns: list[ConversationTurn]
