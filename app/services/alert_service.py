from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.alert_event import AlertEvent
from app.models.alert_subscription import AlertSubscription
from app.models.api_client_key import ApiClientKey
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.billing_service import BillingService
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.provider_service import ProviderService
from app.services.system_metrics_service import SystemMetricsService
from app.utils.json_utils import dumps_json, safeJsonParse


class AlertService:
    DASHBOARD_CACHE_KEY = "alerts-dashboard-payload:v1"
    DASHBOARD_CACHE_TTL_SECONDS = 15
    ALERT_USER_CANDIDATE_LIMIT = 500
    ALERT_PROVIDER_EVENT_LIMIT = 100
    ALERT_EVENT_LIST_LIMIT = 500
    ALERT_RESOLVE_BATCH_SIZE = 500
    ALERT_RESOLVE_MAX_BATCHES = 100
    PROVIDER_AVAILABILITY_LABELS = {
        "healthy": "全部可用",
        "degraded": "部分可用",
        "unhealthy": "全部不可用",
        "unknown": "未检测",
    }
    CIRCUIT_STATE_LABELS = {
        "closed": "闭合",
        "open": "已熔断",
        "half_open": "半开探测",
        "unknown": "未知",
    }

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
    def serialize_subscription(subscription: AlertSubscription) -> dict:
        return {
            "enabled": subscription.enabled,
            "delivery_channel": subscription.delivery_channel,
            "notify_provider_alerts": subscription.notify_provider_alerts,
            "notify_api_key_alerts": subscription.notify_api_key_alerts,
            "notify_account_alerts": subscription.notify_account_alerts,
            "notify_failure_rate_alerts": subscription.notify_failure_rate_alerts,
            "browser_notifications_enabled": subscription.browser_notifications_enabled,
            "poll_interval_seconds": subscription.poll_interval_seconds,
        }

    @staticmethod
    def invalidate_dashboard_cache() -> None:
        CacheService.invalidate_prefix(AlertService.DASHBOARD_CACHE_KEY)

    @staticmethod
    def build_dashboard_payload(db: Session, *, force_refresh: bool = False) -> dict:
        if not force_refresh:
            cached = CacheService.get(AlertService.DASHBOARD_CACHE_KEY)
            if cached is not None:
                return cached
        snapshot = AlertService.refresh_events(db)
        payload = {
            **snapshot,
            "events": AlertService.list_events(db, status="active", limit=50),
        }
        CacheService.set(
            AlertService.DASHBOARD_CACHE_KEY,
            payload,
            ttl_seconds=AlertService.DASHBOARD_CACHE_TTL_SECONDS,
        )
        return payload

    @staticmethod
    def refresh_events(db: Session) -> dict:
        providers = ProviderService.list_provider_summary_dicts(db)
        unhealthy_providers = [
            item for item in providers
            if item["health_status"] != "healthy" or item["circuit_state"] == "open"
        ][:AlertService.ALERT_PROVIDER_EVENT_LIMIT]
        abnormal_api_keys = AlertService.list_abnormal_api_keys(db, limit=100)
        alert_users = AlertService.list_alert_users(db, limit=100)
        recent_since = now_beijing() - timedelta(hours=24)
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
            availability_label = AlertService.PROVIDER_AVAILABILITY_LABELS.get(item["health_status"], "未检测")
            circuit_label = AlertService.CIRCUIT_STATE_LABELS.get(item["circuit_state"], "未知")
            active_events[alert_key] = {
                "alert_key": alert_key,
                "alert_type": "provider",
                "severity": "danger" if item["circuit_state"] == "open" or item["health_status"] == "unhealthy" else "warning",
                "title": f"提供商可用性异常 · {item['name']}",
                "message": f"整体可用性 {availability_label}，熔断状态 {circuit_label}",
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
        system_metrics = SystemMetricsService.collect(db, window_minutes=5, refresh_alerts=False)
        for item in system_metrics.get("alerts", []):
            active_events[item["alert_key"]] = item

        active_events = {
            key: AlertService._normalize_alert_event_payload(value)
            for key, value in active_events.items()
        }
        SystemMetricsService.apply_monitoring_alert_actions(db, active_events, auto_commit=False)
        AlertService._upsert_events(db, active_events)
        return {
            "unhealthy_providers": unhealthy_providers[:20],
            "abnormal_api_keys": abnormal_api_keys[:20],
            "alert_users": alert_users[:20],
            "failure_count": failure_count,
            "request_count": request_count,
            "failure_rate": failure_rate,
            "system_metrics": system_metrics,
        }

    @staticmethod
    def list_abnormal_api_keys(db: Session, *, limit: int = 100) -> list[dict]:
        now = now_beijing()
        status_expr = ApiKeyAdminService._api_key_status_expr(now)
        rows = db.execute(
            select(
                ApiClientKey,
                status_expr.label("status"),
                UserAccount.username.label("owner_username"),
                UserAccount.balance_amount.label("owner_balance_amount"),
            )
            .outerjoin(UserAccount, ApiClientKey.owner_user_id == UserAccount.id)
            .where(status_expr != "active")
            .order_by(ApiClientKey.last_used_at.desc(), ApiClientKey.id.desc())
            .limit(max(1, limit))
        ).all()
        items: list[dict] = []
        for api_key, status, owner_username, owner_balance_amount in rows:
            items.append(
                {
                    "id": api_key.id,
                    "name": api_key.name,
                    "owner_user_name": owner_username,
                    "status": status,
                    "balance_amount": (
                        BillingService.to_float(owner_balance_amount)
                        if owner_balance_amount is not None
                        else None
                    ),
                    "last_used_at": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
                }
            )
        return items

    @staticmethod
    def list_alert_users(db: Session, *, limit: int = 100) -> list[dict]:
        users = list(
            db.scalars(
                select(UserAccount)
                .where(
                    UserAccount.enabled.is_(True),
                    or_(
                        UserAccount.balance_amount <= 0,
                        (
                            (UserAccount.balance_amount > 0)
                            & (
                                (UserAccount.balance_amount - UserAccount.frozen_amount)
                                <= (UserAccount.balance_amount * 0.2)
                            )
                        ),
                    ),
                )
                .order_by(UserAccount.balance_amount.asc(), UserAccount.id.asc())
                .limit(AlertService.ALERT_USER_CANDIDATE_LIMIT)
            )
        )
        if not users:
            return []

        user_ids = [item.id for item in users]
        now = now_beijing()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        usage_rows = db.execute(
            select(
                RequestLog.user_account_id.label("user_id"),
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.created_at >= day_start, 1), else_=0)).label("day_requests"),
                func.sum(case((RequestLog.created_at >= month_start, 1), else_=0)).label("month_requests"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
                func.sum(case((RequestLog.created_at >= day_start, RequestLog.total_tokens), else_=0)).label("day_tokens"),
                func.sum(case((RequestLog.created_at >= month_start, RequestLog.total_tokens), else_=0)).label("month_tokens"),
            )
            .where(
                RequestLog.user_account_id.in_(user_ids),
                LogService._route_traffic_expr(),
                LogService._non_model_list_request_expr(),
            )
            .group_by(RequestLog.user_account_id)
        ).all()
        usage_by_user_id = {
            int(row.user_id): {
                "total_requests": int(row.total_requests or 0),
                "day_requests": int(row.day_requests or 0),
                "month_requests": int(row.month_requests or 0),
                "total_tokens": int(row.total_tokens or 0),
                "day_tokens": int(row.day_tokens or 0),
                "month_tokens": int(row.month_tokens or 0),
            }
            for row in usage_rows
            if row.user_id is not None
        }

        abnormal_key_rows = db.execute(
            select(ApiClientKey.owner_user_id, func.count(ApiClientKey.id).label("abnormal_count"))
            .outerjoin(UserAccount, ApiClientKey.owner_user_id == UserAccount.id)
            .where(
                ApiClientKey.owner_user_id.in_(user_ids),
                ApiKeyAdminService._api_key_status_expr(now) != "active",
            )
            .group_by(ApiClientKey.owner_user_id)
        ).all()
        abnormal_key_count_by_user_id = {
            int(row[0]): int(row.abnormal_count or 0)
            for row in abnormal_key_rows
            if row[0] is not None
        }

        alert_users: list[dict] = []
        for user in users:
            usage = usage_by_user_id.get(user.id, {})
            account_summary = {
                "balance_amount": BillingService.to_float(user.balance_amount),
                "available_balance": BillingService.to_float((user.balance_amount or 0) - (user.frozen_amount or 0)),
                "day_requests": usage.get("day_requests", 0),
                "month_requests": usage.get("month_requests", 0),
                "day_tokens": usage.get("day_tokens", 0),
                "month_tokens": usage.get("month_tokens", 0),
            }
            warnings = AlertService._build_account_warnings(
                account_summary=account_summary,
                abnormal_key_count=abnormal_key_count_by_user_id.get(user.id, 0),
            )
            if warnings:
                alert_users.append(
                    {
                        "id": user.id,
                        "username": user.username,
                        "warnings": warnings,
                        "available_balance": account_summary["available_balance"],
                    }
                )

        alert_users.sort(
            key=lambda item: (
                0 if any(warning.get("level") == "danger" for warning in item["warnings"]) else 1,
                item.get("available_balance") if item.get("available_balance") is not None else float("inf"),
                item["id"],
            )
        )
        return alert_users[:max(1, limit)]

    @staticmethod
    def _build_account_warnings(*, account_summary: dict, abnormal_key_count: int) -> list[dict]:
        warnings: list[dict] = []
        available_balance = account_summary.get("available_balance")
        balance_amount = account_summary.get("balance_amount")
        if available_balance is not None and available_balance <= 0:
            warnings.append({"level": "danger", "message": "账户可用余额已耗尽，新请求会被拦截。"})
        elif (
            available_balance is not None
            and balance_amount not in (None, 0)
            and balance_amount
            and available_balance / balance_amount <= 0.2
        ):
            warnings.append({"level": "warning", "message": "账户可用余额已低于 20%，建议尽快补充额度。"})

        if abnormal_key_count > 0:
            warnings.append({"level": "warning", "message": f"当前有 {abnormal_key_count} 个 API Key 处于非正常状态，建议及时处理。"})
        return warnings

    @staticmethod
    def upsert_alert(
        db: Session,
        *,
        alert_key: str,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        payload: dict | None = None,
        status: str = "active",
        auto_commit: bool = True,
    ) -> AlertEvent:
        now = now_beijing()
        item = db.scalar(select(AlertEvent).where(AlertEvent.alert_key == alert_key))
        safe_payload = AlertService._json_safe(payload or {})
        if item is None:
            item = AlertEvent(
                alert_key=alert_key,
                alert_type=alert_type,
                severity=severity,
                title=title,
                message=message,
                payload_json=dumps_json(safe_payload),
                status=status or "active",
                first_seen_at=now,
                last_seen_at=now,
            )
            db.add(item)
        else:
            item.alert_type = alert_type
            item.severity = severity
            item.title = title
            item.message = message
            item.payload_json = dumps_json(safe_payload)
            item.status = status or "active"
            item.last_seen_at = now
            if item.status == "active":
                item.resolved_at = None
        if auto_commit:
            db.commit()
            db.refresh(item)
            AlertService.invalidate_dashboard_cache()
        return item

    @staticmethod
    def _upsert_events(db: Session, active_events: dict[str, dict]) -> None:
        active_keys = set(active_events.keys())
        existing_items = AlertService._load_active_alert_events(db, active_keys)
        existing_by_key = {item.alert_key: item for item in existing_items}
        now = now_beijing()
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
        changed = AlertService._resolve_inactive_alert_events(db, active_keys, now=now) or changed
        if changed:
            db.commit()

    @staticmethod
    def _load_active_alert_events(db: Session, active_keys: set[str]) -> list[AlertEvent]:
        if not active_keys:
            return []
        return list(db.scalars(select(AlertEvent).where(AlertEvent.alert_key.in_(active_keys))))

    @staticmethod
    def _resolve_inactive_alert_events(db: Session, active_keys: set[str], *, now: datetime) -> bool:
        changed = False
        for _ in range(AlertService.ALERT_RESOLVE_MAX_BATCHES):
            query = select(AlertEvent).where(AlertEvent.status != "resolved")
            if active_keys:
                query = query.where(AlertEvent.alert_key.not_in(active_keys))
            stale_items = list(
                db.scalars(
                    query.order_by(AlertEvent.id.asc()).limit(AlertService.ALERT_RESOLVE_BATCH_SIZE)
                )
            )
            if not stale_items:
                break
            for item in stale_items:
                item.status = "resolved"
                item.resolved_at = now
                changed = True
            if len(stale_items) < AlertService.ALERT_RESOLVE_BATCH_SIZE:
                break
        return changed

    @staticmethod
    def _normalize_alert_event_payload(event: dict) -> dict:
        alert_key = str(event.get("alert_key") or "")
        alert_type = str(event.get("alert_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        normalized_type = alert_type
        typed_payload: dict[str, Any] = {"raw": payload}
        if alert_key == "monitoring:redis_unavailable":
            normalized_type = "redis"
            typed_payload.update({
                "redis_status": payload.get("status") or ("ok" if payload.get("ok") else "unavailable"),
                "latency_ms": payload.get("latency_ms"),
                "active_requests": payload.get("active_requests"),
                "active_streams": payload.get("active_streams"),
                "error": payload.get("error"),
            })
        elif alert_key == "monitoring:database_unavailable":
            normalized_type = "database"
            typed_payload.update({
                "database_status": payload.get("status") or ("ok" if payload.get("ok") else "unavailable"),
                "dialect": payload.get("dialect"),
                "pool_status": payload.get("pool"),
                "latency_ms": payload.get("latency_ms"),
                "error": payload.get("error"),
            })
        elif alert_key in {"monitoring:global_active_requests", "monitoring:global_active_streams"}:
            normalized_type = "concurrency"
            active = payload.get("active_requests") if "requests" in alert_key else payload.get("active_streams")
            typed_payload.update({
                "scope": "active_requests" if "requests" in alert_key else "active_streams",
                "active": active,
                "limit": payload.get("limit") or payload.get("max_active_requests") or payload.get("max_active_streams"),
                "threshold": payload.get("threshold"),
            })
        elif alert_key == "monitoring:background_backlog":
            normalized_type = "queue"
            typed_payload.update({
                "queue_name": "token_finalize",
                "queued": payload.get("pending_finalize_logs"),
                "processing": payload.get("processing_finalize_logs"),
                "threshold": SystemMetricsService.BACKGROUND_BACKLOG_WARNING_THRESHOLD,
            })
        elif alert_key in {"monitoring:status_5xx_rate", "monitoring:status_429_spike", "failure_rate:24h"}:
            normalized_type = "traffic"
            typed_payload.update({
                "window_minutes": payload.get("window_minutes") or 1440,
                "total_requests": payload.get("total_requests") or payload.get("request_count"),
                "error_count": payload.get("status_5xx") or payload.get("status_429") or payload.get("failure_count"),
                "error_rate": payload.get("status_5xx_rate") or payload.get("status_429_rate") or payload.get("failure_rate"),
            })
        elif alert_key.startswith("monitoring:provider_failure_rate"):
            normalized_type = "provider"
            typed_payload.update({
                "provider_id": payload.get("provider_id"),
                "provider_name": payload.get("provider_name"),
                "failure_rate": payload.get("failure_rate"),
                "total_requests": payload.get("total_requests"),
            })
        elif alert_key.startswith("monitoring:content_guard_high_risk_provider"):
            normalized_type = "content_risk"
            typed_payload.update({
                "provider_id": payload.get("provider_id"),
                "provider_name": payload.get("provider_name"),
                "high_risk_count": payload.get("high_risk_count"),
                "window_minutes": payload.get("window_minutes"),
                "auto_isolated": payload.get("auto_isolated"),
                "isolation_status": payload.get("isolation_status"),
                "isolation_failed": payload.get("isolation_failed"),
                "isolation_error": payload.get("isolation_error"),
            })
        elif alert_key == "monitoring:billing_failed":
            normalized_type = "billing"
            typed_payload.update({
                "request_log_id": payload.get("request_log_id"),
                "trace_id": payload.get("trace_id"),
                "attempt_count": payload.get("billing_attempt_count"),
                "error": payload.get("billing_error") or payload.get("error"),
                "failed_count": payload.get("billing_failed_logs"),
            })
        elif alert_key == "monitoring:token_finalize_failed":
            normalized_type = "queue"
            typed_payload.update({
                "queue_name": "token_finalize",
                "queued": payload.get("pending_finalize_logs"),
                "failed": payload.get("token_failed_logs"),
                "threshold": SystemMetricsService.TOKEN_FAILURE_WARNING_THRESHOLD,
            })
        elif alert_type == "provider":
            typed_payload.update({
                "provider_id": payload.get("id") or payload.get("provider_id"),
                "provider_name": payload.get("name") or payload.get("provider_name"),
                "failure_rate": payload.get("failure_rate"),
                "total_requests": payload.get("total_requests"),
                "health_status": payload.get("health_status"),
                "circuit_state": payload.get("circuit_state"),
            })
        elif alert_type == "api_key":
            typed_payload.update({
                "api_client_key_id": payload.get("id"),
                "api_client_key_name": payload.get("name"),
                "status": payload.get("status"),
                "owner_user_name": payload.get("owner_user_name"),
                "balance_amount": payload.get("balance_amount"),
            })
        elif alert_type == "account":
            typed_payload.update({
                "user_account_id": payload.get("id"),
                "username": payload.get("username"),
                "warnings": payload.get("warnings"),
                "available_balance": payload.get("available_balance"),
            })
        normalized = dict(event)
        normalized["alert_type"] = normalized_type
        normalized["payload"] = typed_payload
        return normalized

    @staticmethod
    def list_events(db: Session, *, status: str = "active", limit: int = 50) -> list[dict]:
        normalized_limit = max(1, min(int(limit or 50), AlertService.ALERT_EVENT_LIST_LIMIT))
        rows = list(
            db.scalars(
                select(AlertEvent)
                .where(AlertEvent.status == status)
                .order_by(AlertEvent.last_seen_at.desc(), AlertEvent.id.desc())
                .limit(normalized_limit)
            )
        )
        return [AlertService.serialize_event(item) for item in rows]

    @staticmethod
    def acknowledge_event(db: Session, *, event_id: int) -> AlertEvent | None:
        item = db.get(AlertEvent, event_id)
        if item is None:
            return None
        item.acknowledged_at = now_beijing()
        db.commit()
        db.refresh(item)
        AlertService.invalidate_dashboard_cache()
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

from app.utils.timezone import now_beijing