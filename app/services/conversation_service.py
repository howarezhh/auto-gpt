from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.request_log import RequestLog
from app.schemas.conversation import ConversationReplay, ConversationSummaryItem, ConversationTurn
from app.utils.json_utils import loads_json


class ConversationService:
    @staticmethod
    def list_conversations(
        db: Session,
        *,
        page: int,
        page_size: int,
        query: str | None = None,
        api_client_key_ids: list[int] | None = None,
    ) -> tuple[int, list[ConversationSummaryItem]]:
        filters = [RequestLog.conversation_key.is_not(None)]
        if api_client_key_ids is not None:
            if not api_client_key_ids:
                return 0, []
            filters.append(RequestLog.api_client_key_id.in_(api_client_key_ids))
        if query:
            like_value = f"%{query.strip()}%"
            filters.append(
                or_(
                    RequestLog.conversation_key.like(like_value),
                    RequestLog.requested_model.like(like_value),
                    RequestLog.provider_name.like(like_value),
                    RequestLog.response_text.like(like_value),
                    RequestLog.message.like(like_value),
                )
            )

        grouped = (
            select(
                RequestLog.conversation_key.label("conversation_key"),
                func.count(RequestLog.id).label("request_count"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
                func.sum(case((RequestLog.success.is_(True), 1), else_=0)).label("success_count"),
                func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failure_count"),
                func.min(RequestLog.created_at).label("started_at"),
                func.max(RequestLog.created_at).label("updated_at"),
            )
            .where(*filters)
            .group_by(RequestLog.conversation_key)
            .subquery()
        )

        total = db.scalar(select(func.count()).select_from(grouped)) or 0
        rows = db.execute(
            select(grouped).order_by(grouped.c.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
        ).all()

        items: list[ConversationSummaryItem] = []
        for row in rows:
            latest_log = db.scalar(
                select(RequestLog)
                .where(RequestLog.conversation_key == row.conversation_key)
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(1)
            )
            preview_text = None
            if latest_log is not None:
                preview_text = latest_log.response_text or latest_log.message
                if preview_text:
                    preview_text = preview_text[:180]
            items.append(
                ConversationSummaryItem(
                    conversation_key=row.conversation_key,
                    request_count=int(row.request_count or 0),
                    success_count=int(row.success_count or 0),
                    failure_count=int(row.failure_count or 0),
                    total_tokens=int(row.total_tokens or 0),
                    started_at=row.started_at,
                    updated_at=row.updated_at,
                    latest_model=latest_log.requested_model if latest_log else None,
                    latest_provider_name=latest_log.provider_name if latest_log else None,
                    preview_text=preview_text,
                )
            )
        return total, items

    @staticmethod
    def get_replay(
        db: Session,
        conversation_key: str,
        *,
        api_client_key_ids: list[int] | None = None,
    ) -> ConversationReplay | None:
        filters = [RequestLog.conversation_key == conversation_key]
        if api_client_key_ids is not None:
            if not api_client_key_ids:
                return None
            filters.append(RequestLog.api_client_key_id.in_(api_client_key_ids))
        logs = list(
            db.scalars(
                select(RequestLog)
                .where(*filters)
                .order_by(RequestLog.created_at.asc(), RequestLog.id.asc())
            )
        )
        if not logs:
            return None

        turns: list[ConversationTurn] = []
        history_messages: list[dict[str, str]] = []
        success_count = 0
        failure_count = 0
        total_tokens = 0

        for log in logs:
            if log.success:
                success_count += 1
            else:
                failure_count += 1
            total_tokens += int(log.total_tokens or 0)

            request_messages = ConversationService._extract_request_messages(log.request_body_json)
            new_messages = ConversationService._diff_messages(history_messages, request_messages)
            for message in new_messages:
                turns.append(
                    ConversationTurn(
                        role=message["role"],
                        content=message["content"],
                        created_at=log.created_at,
                        request_id=log.request_id,
                        requested_model=log.requested_model,
                        provider_name=log.provider_name,
                        total_tokens=log.total_tokens,
                        log_id=log.id,
                        is_stream=log.is_stream,
                        has_image=log.has_image,
                    )
                )

            if log.response_text:
                turns.append(
                    ConversationTurn(
                        role="assistant",
                        content=log.response_text,
                        created_at=log.created_at,
                        request_id=log.request_id,
                        requested_model=log.requested_model,
                        provider_name=log.provider_name,
                        total_tokens=log.total_tokens,
                        log_id=log.id,
                        is_stream=log.is_stream,
                        has_image=log.has_image,
                    )
                )
                history_messages = request_messages + [{"role": "assistant", "content": log.response_text}]
            else:
                history_messages = request_messages

        latest_log = logs[-1]
        return ConversationReplay(
            conversation_key=conversation_key,
            request_count=len(logs),
            success_count=success_count,
            failure_count=failure_count,
            total_tokens=total_tokens,
            started_at=logs[0].created_at,
            updated_at=logs[-1].created_at,
            latest_model=latest_log.requested_model,
            latest_provider_name=latest_log.provider_name,
            turns=turns,
        )

    @staticmethod
    def _extract_request_messages(request_body_json: str | None) -> list[dict[str, str]]:
        payload = loads_json(request_body_json, {})
        if not isinstance(payload, dict):
            return []

        if isinstance(payload.get("messages"), list):
            return ConversationService._normalize_messages(payload["messages"])

        if isinstance(payload.get("input"), list):
            return ConversationService._normalize_messages(payload["input"])

        if isinstance(payload.get("input"), str):
            return [{"role": "user", "content": payload["input"]}]

        if isinstance(payload.get("prompt"), str):
            return [{"role": "user", "content": payload["prompt"]}]

        return []

    @staticmethod
    def _normalize_messages(messages: Iterable[Any]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for message in messages:
            if isinstance(message, str):
                normalized.append({"role": "user", "content": message})
                continue
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("type") or "user")
            content = ConversationService._normalize_content(message.get("content"))
            if not content and isinstance(message.get("input_text"), str):
                content = message["input_text"]
            if content:
                normalized.append({"role": role, "content": content})
        return normalized

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type in {"text", "input_text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item_type in {"image_url", "input_image"}:
                    image_url = item.get("image_url")
                    if isinstance(image_url, dict):
                        image_url = image_url.get("url")
                    parts.append(f"[image] {image_url or 'attached'}")
                elif isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "\n".join(part for part in parts if part).strip()
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return content["text"]
        return ""

    @staticmethod
    def _diff_messages(history_messages: list[dict[str, str]], request_messages: list[dict[str, str]]) -> list[dict[str, str]]:
        common = 0
        max_common = min(len(history_messages), len(request_messages))
        while common < max_common and history_messages[common] == request_messages[common]:
            common += 1
        if common < len(request_messages):
            return request_messages[common:]
        if request_messages:
            return [request_messages[-1]]
        return []
