class AlertLogRecorder:
    """告警仍由 AlertService 维护幂等 upsert，本类作为统一日志模块注册入口。"""

    @staticmethod
    def upsert_alert(*args, **kwargs):
        from app.services.alert_service import AlertService

        return AlertService.upsert_alert(*args, **kwargs)
