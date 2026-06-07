from enum import StrEnum


class LogEventType(StrEnum):
    EXTERNAL_REQUEST = "external_request"
    EXCEPTION = "exception"
    HEALTH_CHECK = "health_check"
    BILLING = "billing"
    BACKGROUND_JOB = "background_job"
    ADMIN_AUDIT = "admin_audit"
    USER_OPERATION = "user_operation"
    ASSET = "asset"
    ALERT = "alert"


class LogSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"
    CRITICAL = "critical"


class LogResult(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
