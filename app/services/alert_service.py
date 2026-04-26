from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.alert_event import AlertEvent
from app.models.alert_subscription import AlertSubscription
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.provider_service import ProviderService
from app.services.user_auth_service import UserAuthService
from app.services.user_portal_service import UserPortalService
from app.utils.json_utils import dumps_json, safeJsonParse


class AlertService:
    @staticmethod
    def _json_safe(value):
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: AlertService._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [AlertService._json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [AlertService._json_safe(item) for item in value]
        return value

    @staticmethod
    def get_or_create_subscription(db: Session, *, user: UserAccount) -> AlertSubscription:
        subscription = db.scalar(
            select(AlertSubscription).where(AlertSubscription.user_account_id == user.id)
        )
        if subscription is not None:
            return subscription
        subscription = AlertSubscription(user_account_id=user.id)
        db.add(subscription)
        db.commit()
        db.refresh(subscription)
        return subscription

    @staticmethod
    def update_subscription(
        db: Session,
        *,
        user: UserAccount,
        enabled: bool,
        notify_provider_alerts: bool,
        notify_api_key_alerts: bool,
        notify_account_alerts: bool,
        notify_failure_rate_alerts: bool,
        browser_notifications_enabled: bool,
        poll_interval_seconds: int,
    ) -> AlertSubscription:
        subscription = AlertService.get_or_create_subscription(db, user=user)
        subscription.enabled = enabled
        subscription.notify_provider_alerts = notify_provider_alerts
        subscription.notify_api_key_alerts = notify_api_key_alerts
        subscription.notify_account_alerts = notify_account_alerts
        subscription.notify_failure_rate_alerts = notify_failure_rate_alerts
        subscription.browser_notifications_enabled = browser_notifications_enabled
        subscription.poll_interval_seconds = max(10, min(300, int(poll_interval_seconds)))
        db.commit()
        db.refresh(subscription)
        return subscription

    @staticmethod
    def build_dashboard_payload(db: Session) -> dict:
        snapshot = AlertService.refresh_events(db)
        return {
            **snapshot,
            "events": AlertService.list_events(db, status="active", limit=50),
        }

    @staticmethod
    def refresh_events(db: Session) -> dict:
        providers = ProviderService.list_provider_dicts(db)
        unhealthy_providers = [
            item for item in providers
            if item["health_status"] != "healthy" or item["circuit_state"] == "open"
        ]
        api_keys = [ApiKeyAdminService.serialize_api_key(item) for item in ApiKeyAdminService.list_api_keys(db)]
        abnormal_api_keys = [item for item in api_keys if item["status"] != "active"]
        alert_users: list[dict] = []
        for user in UserAuthService.list_users(db):
            if not user.enabled:
                continue
            overview = UserPortalService.get_overview(db, user=user)
            if overview["quota_warnings"]:
                alert_users.append(
                    {
                        "id": user.id,
                        "username": user.username,
                        "warnings": overview["quota_warnings"],
                        "available_balance": overview["account_summary"]["available_balance"],
                    }
                )
        recent_since = datetime.utcnow() - timedelta(hours=24)
        failure_count = int(
            db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    RequestLog.created_at >= recent_since,
                    RequestLog.success.is_(False),
                )
            ) or 0
        )
        request_count = int(
            db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since)) or 0
        )
        failure_rate = round((failure_count / request_count) * 100, 2) if request_count else 0.0

        active_events: dict[str, dict] = {}
        for item in unhealthy_providers:
            alert_key = f"provider:{item['id']}"
            active_events[alert_key] = {
                "alert_key": alert_key,
                "alert_type": "provider",
                "severity": "danger" if item["circuit_state"] == "open" or item["health_status"] == "unhealthy" else "warning",
                "title": f"渠道异常 · {item['name']}",
                "message": f"健康状态 {item['health_status']}，熔断状态 {item['circuit_state']}",
                "payload": item,
            }
        for item in abnormal_api_keys[:100]:
            alert_key = f"api_key:{item['id']}"
            active_events[alert_key] = {
                "alert_key": alert_key,
                "alert_type": "api_key",
                "severity": "warning",
                "title": f"API Key 异常 · {item['name']}",
                "message": f"当前状态 {item['status']}",
                "payload": item,
            }
        for item in alert_users[:100]:
            alert_key = f"user:{item['id']}"
            active_events[alert_key] = {
                "alert_key": alert_key,
                "alert_type": "account",
                "severity": "warning",
                "title": f"账户预警 · {item['username']}",
                "message": item["warnings"][0]["message"],
                "payload": item,
            }
        if failure_rate >= 20 and request_count >= 10:
            alert_key = "failure_rate:24h"
            active_events[alert_key] = {
                "alert_key": alert_key,
                "alert_type": "failure_rate",
                "severity": "danger" if failure_rate >= 40 else "warning",
                "title": "24h 失败率过高",
                "message": f"最近 24 小时失败率 {failure_rate:.2f}%（{failure_count}/{request_count}）",
                "payload": {
                    "failure_count": failure_count,
                    "request_count": request_count,
                    "failure_rate": failure_rate,
                },
            }

        AlertService._upsert_events(db, active_events)
        return {
            "unhealthy_providers": unhealthy_providers[:20],
            "abnormal_api_keys": abnormal_api_keys[:20],
            "alert_users": alert_users[:20],
            "failure_count": failure_count,
            "request_count": request_count,
            "failure_rate": failure_rate,
        }

    @staticmethod
    def _upsert_events(db: Session, active_events: dict[str, dict]) -> None:
        existing_items = list(db.scalars(select(AlertEvent)))
        existing_by_key = {item.alert_key: item for item in existing_items}
        now = datetime.utcnow()
        changed = False
        for alert_key, payload in active_events.items():
            safe_payload = AlertService._json_safe(payload["payload"])
            item = existing_by_key.get(alert_key)
            if item is None:
                item = AlertEvent(
                    alert_key=alert_key,
                    alert_type=payload["alert_type"],
                    severity=payload["severity"],
                    title=payload["title"],
                    message=payload["message"],
                    payload_json=dumps_json(safe_payload),
                    status="active",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                db.add(item)
                changed = True
                continue
            item.alert_type = payload["alert_type"]
            item.severity = payload["severity"]
            item.title = payload["title"]
            item.message = payload["message"]
            item.payload_json = dumps_json(safe_payload)
            item.status = "active"
            item.last_seen_at = now
            item.resolved_at = None
            changed = True
        active_keys = set(active_events.keys())
        for item in existing_items:
            if item.alert_key in active_keys or item.status == "resolved":
                continue
            item.status = "resolved"
            item.resolved_at = now
            changed = True
        if changed:
            db.commit()

    @staticmethod
    def list_events(db: Session, *, status: str = "active", limit: int = 50) -> list[dict]:
        rows = list(
            db.scalars(
                select(AlertEvent)
                .where(AlertEvent.status == status)
                .order_by(AlertEvent.last_seen_at.desc(), AlertEvent.id.desc())
                .limit(max(1, limit))
            )
        )
        return [AlertService.serialize_event(item) for item in rows]

    @staticmethod
    def acknowledge_event(db: Session, *, event_id: int) -> AlertEvent | None:
        item = db.get(AlertEvent, event_id)
        if item is None:
            return None
        item.acknowledged_at = datetime.utcnow()
        db.commit()
        db.refresh(item)
        return item

    @staticmethod
    def serialize_event(item: AlertEvent) -> dict:
        return {
            "id": item.id,
            "alert_key": item.alert_key,
            "alert_type": item.alert_type,
            "severity": item.severity,
            "title": item.title,
            "message": item.message,
            "payload": safeJsonParse(item.payload_json or "") or item.payload_json,
            "status": item.status,
            "first_seen_at": item.first_seen_at.isoformat() if item.first_seen_at else None,
            "last_seen_at": item.last_seen_at.isoformat() if item.last_seen_at else None,
            "last_notified_at": item.last_notified_at.isoformat() if item.last_notified_at else None,
            "acknowledged_at": item.acknowledged_at.isoformat() if item.acknowledged_at else None,
            "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
        }
