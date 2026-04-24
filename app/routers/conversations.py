from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.conversation import ConversationReplay, ConversationSummaryList
from app.services.conversation_service import ConversationService


router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("", response_model=ConversationSummaryList)
def list_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    query: str | None = None,
    db: Session = Depends(get_db),
) -> ConversationSummaryList:
    total, items = ConversationService.list_conversations(db, page=page, page_size=page_size, query=query)
    return ConversationSummaryList(total=total, items=items)


@router.get("/{conversation_key}", response_model=ConversationReplay)
def get_conversation(conversation_key: str, db: Session = Depends(get_db)) -> ConversationReplay:
    replay = ConversationService.get_replay(db, conversation_key)
    if replay is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return replay
