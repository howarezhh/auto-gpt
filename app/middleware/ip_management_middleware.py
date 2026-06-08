from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import SessionLocal
from app.services.ip_management_service import IpManagementService
from app.services.openai_error_service import OpenAIErrorService

logger = logging.getLogger(__name__)


class IpManagementMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        db = SessionLocal()
        try:
            decision = await IpManagementService.evaluate_request(db, request)
            if decision is not None:
                request.state.ip_management = decision
            if decision is not None and decision.should_block:
                trace_id = getattr(request.state, "trace_id", None)
                if decision.scope == "external_v1":
                    return JSONResponse(
                        status_code=decision.status_code or 403,
                        content=OpenAIErrorService.build_error_payload(
                            message=decision.message or "来源 IP 已被拒绝。",
                            code=decision.error_code or "source_ip_blocked",
                            trace_id=trace_id,
                            status_code=decision.status_code or 403,
                            retryable=decision.status_code == 429,
                            recoverable=decision.status_code == 429,
                            category="rate_limit" if decision.status_code == 429 else "authorization",
                        ),
                        headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
                    )
                return JSONResponse(
                    status_code=decision.status_code or 403,
                    content={
                        "detail": {
                            "code": decision.error_code,
                            "message": decision.message,
                            "trace_id": trace_id,
                        }
                    },
                    headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
                )
        except Exception as exc:
            logger.warning("IP 管理模块执行失败: %s", exc)
            fail_open = True
            try:
                fail_open = bool(IpManagementService.get_cached_setting(db).fail_open_enabled)
            except Exception:
                fail_open = True
            if not fail_open:
                trace_id = getattr(request.state, "trace_id", None)
                if request.url.path == "/v1" or request.url.path.startswith("/v1/"):
                    return JSONResponse(
                        status_code=400,
                        content=OpenAIErrorService.build_error_payload(
                            message="服务端无法安全解析当前请求来源 IP。",
                            code="source_ip_resolution_failed",
                            trace_id=trace_id,
                            status_code=400,
                            retryable=False,
                            recoverable=False,
                            category="invalid_request",
                        ),
                        headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
                    )
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": {
                            "code": "source_ip_resolution_failed",
                            "message": "服务端无法安全解析当前请求来源 IP。",
                            "trace_id": trace_id,
                        }
                    },
                    headers={"X-Trace-Id": trace_id or "", "X-Request-Id": trace_id or ""},
                )
        finally:
            db.close()
        return await call_next(request)
