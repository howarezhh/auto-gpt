import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.logging.adapters.request_adapter import RequestLogRecorder
from app.models.logging_events import RequestProviderAttemptEvent
from app.models.request_log import RequestLog
from app.routers.logging_api import build_request_log_timeline_payload
from app.services.log_service import LogService
from app.services.proxy_service import ProxyService
from app.services.request_log_queue_service import RequestLogQueueService
from app.utils.json_utils import dumps_json, safeJsonParse


class RequestLoggingObservabilityRegressionTest(unittest.TestCase):
    def test_route_exhausted_trace_does_not_backfill_fake_provider_attempt(self):
        log = RequestLog(
            id=1001,
            log_type="responses",
            trace_id="trace-route-exhausted",
            request_path="/v1/responses",
            requested_model="gpt-5.5",
            model_name="gpt-5.5",
            success=False,
            error_code="all_providers_unavailable_after_retry",
            trace_json=dumps_json(
                [
                    {
                        "result": "route_candidates_exhausted",
                        "diagnostic": {"final_candidate_count": 1, "reason_counts": {"route_outage": 1}},
                        "hard_filter_final_candidate_count": 1,
                        "failed_candidate_count": 1,
                        "failed_candidate_keys": [{"provider_id": 23, "provider_model_id": 45}],
                        "candidate_count_after_failed_exclusion": 0,
                    },
                    {
                        "result": "request_rejected_before_route",
                        "provider_id": 23,
                        "provider_name": "聚合",
                    },
                ]
            ),
        )

        events = RequestLogRecorder.build_events_from_summary(log)
        provider_attempts = [
            event for event in events if event.envelope.event_name == "request_provider_attempt"
        ]
        route_events = [
            event for event in events if event.envelope.event_name == "request_route_decision"
        ]

        self.assertEqual(provider_attempts, [])
        self.assertEqual(route_events[-1].payload["candidate_count"], 0)

    def test_attempt_count_ignores_route_wait_retries_without_upstream_attempt(self):
        trace = [
            {"result": "route_exhausted_wait_retry", "route_round": 1},
            {"result": "route_exhausted_wait_retry", "route_round": 2},
        ]

        self.assertEqual(LogService.resolve_attempt_count(attempt_count=17, trace=trace), 0)

    def test_stream_progress_events_do_not_count_as_provider_attempts(self):
        trace = [
            {"result": "connecting", "provider_id": 3, "provider_model_id": 1},
            {"result": "http_error", "provider_id": 3, "provider_model_id": 1},
            {"result": "same_provider_retry_wait", "provider_id": 3, "provider_model_id": 1},
            {"result": "http_error", "provider_id": 3, "provider_model_id": 1},
            {"result": "stream_opened", "provider_id": 28, "provider_model_id": 88},
            {"result": "first_chunk_received", "provider_id": 28, "provider_model_id": 88},
            {"result": "finished", "provider_id": 28, "provider_model_id": 88},
            {"result": "success", "provider_id": 28, "provider_model_id": 88},
        ]

        self.assertEqual(LogService.resolve_attempt_count(attempt_count=None, trace=trace), 3)

    def test_native_provider_attempt_events_skip_progress_markers(self):
        log = RequestLog(
            id=1008,
            log_type="chat",
            trace_id="trace-provider-progress",
            request_path="/v1/chat/completions",
            success=True,
            trace_json=dumps_json(
                [
                    {
                        "typed_event": "request_provider_attempt",
                        "payload": {"provider_id": 3, "provider_model_id": 1, "result": "connecting"},
                    },
                    {
                        "typed_event": "request_provider_attempt",
                        "payload": {"provider_id": 3, "provider_model_id": 1, "result": "http_error"},
                    },
                    {
                        "typed_event": "request_provider_attempt",
                        "payload": {"provider_id": 3, "provider_model_id": 1, "result": "same_provider_retry_wait"},
                    },
                    {
                        "typed_event": "request_provider_attempt",
                        "payload": {"provider_id": 3, "provider_model_id": 1, "result": "http_error"},
                    },
                    {
                        "typed_event": "request_provider_attempt",
                        "payload": {"provider_id": 28, "provider_model_id": 88, "result": "stream_opened"},
                    },
                    {
                        "typed_event": "request_provider_attempt",
                        "payload": {"provider_id": 28, "provider_model_id": 88, "result": "success"},
                    },
                ]
            ),
        )

        provider_events = [
            event for event in RequestLogRecorder.build_events_from_summary(log)
            if event.envelope.event_name == "request_provider_attempt"
        ]

        self.assertEqual([event.payload["result"] for event in provider_events], ["http_error", "http_error", "success"])
        self.assertEqual([event.payload["attempt_index"] for event in provider_events], [1, 2, 3])

    def test_stream_ttfb_is_measured_from_request_entry(self):
        self.assertEqual(
            ProxyService._stream_ttfb_ms(
                request_started_at=100.0,
                attempt_started_at=102.5,
                first_chunk_latency_ms=750,
            ),
            3250,
        )
        self.assertEqual(
            ProxyService._stream_ttfb_ms(
                request_started_at=None,
                attempt_started_at=102.5,
                first_chunk_latency_ms=750,
            ),
            750,
        )

    def test_recent_runtime_ttfb_uses_provider_first_token_latency(self):
        item = LogService._empty_recent_runtime_metrics(
            provider_id=1,
            provider_model_id=2,
            window_seconds=5,
        )
        row = SimpleNamespace(
            provider_id=1,
            resolved_provider_model_id=2,
            model_name="gpt-5.4",
            requested_model="gpt-5.4",
            success=True,
            status_code=200,
            latency_ms=900,
            first_token_latency_ms=400,
            ttfb_ms=2400,
            error_code=None,
            message=None,
            trace_id="trace-runtime-ttfb",
            created_at=datetime(2026, 6, 18, 18, 0, 0),
            content_guard_result=None,
            content_guard_risk_level=None,
        )

        LogService._accumulate_recent_runtime_metric(item, row)
        LogService._finalize_recent_runtime_metric(item)

        self.assertEqual(item["avg_ttfb_ms"], 400)

    def test_serialize_logs_can_include_raw_api_key_for_user_detail(self):
        log = RequestLog(
            id=1002,
            trace_id="trace-user-detail",
            request_path="/v1/chat/completions",
            requested_model="gpt-5.4",
            model_name="gpt-5.4",
            api_client_key_id=42,
            success=True,
        )

        [serialized] = LogService.serialize_logs(
            [log],
            raw_api_key_by_id={42: "sk-user-detail-raw"},
        )

        self.assertEqual(serialized["raw_api_key"], "sk-user-detail-raw")
        self.assertTrue(hasattr(LogService, "load_raw_api_keys_for_logs"))

    def test_success_typed_events_are_deduplicated_across_route_retries(self):
        trace = []
        auth_context = SimpleNamespace(
            api_client_key=SimpleNamespace(id=12, key_prefix="ak_test", owner_user_id=34),
            policy_snapshot_json="{}",
        )

        ProxyService._append_auth_trace_event(trace, auth_context)
        ProxyService._append_auth_trace_event(trace, auth_context)
        ProxyService._append_validation_trace_event(trace, stage="request_body", passed=True)
        ProxyService._append_validation_trace_event(trace, stage="request_body", passed=True)
        ProxyService._append_model_permission_trace_event(
            trace,
            requested_model="gpt-5.5",
            resolved_model="gpt-5.5",
            endpoint_path="/responses",
            permission_result="allowed",
            required_capabilities_json="{}",
        )
        ProxyService._append_model_permission_trace_event(
            trace,
            requested_model="gpt-5.5",
            resolved_model="gpt-5.5",
            endpoint_path="/responses",
            permission_result="allowed",
            required_capabilities_json="{}",
        )

        event_names = [item.get("typed_event") for item in trace]
        self.assertEqual(event_names.count("request_auth"), 1)
        self.assertEqual(event_names.count("request_validation"), 1)
        self.assertEqual(event_names.count("request_model_permission"), 1)

    def test_native_protocol_logs_are_user_visible_route_traffic(self):
        self.assertIn("gemini", LogService.USER_VISIBLE_LOG_TYPES)
        self.assertIn("claude_messages", LogService.USER_VISIBLE_LOG_TYPES)
        self.assertIn("gemini", LogService.ROUTE_TRAFFIC_LOG_TYPES)
        self.assertIn("claude_messages", LogService.ROUTE_TRAFFIC_LOG_TYPES)
        self.assertEqual(LogService.format_csv_label("gemini"), "Gemini 原生请求")
        self.assertEqual(LogService.format_csv_label("claude_messages"), "Claude 原生请求")

    def test_native_billing_trace_uses_final_request_log_status(self):
        log = RequestLog(
            id=1007,
            log_type="chat",
            trace_id="trace-final-billing-status",
            request_path="/v1/chat/completions",
            success=True,
            billable=True,
            token_source="upstream_usage",
            billing_event_id="billing-final-1007",
            billing_status="billed",
            billing_attempt_count=1,
            prompt_tokens=12,
            completion_tokens=3,
            total_tokens=15,
            total_cost=0.000123,
            trace_json=dumps_json(
                [
                    {
                        "typed_event": "request_billing",
                        "module": "billing",
                        "result": "success",
                        "payload": {
                            "billing_stage": "queued",
                            "token_source": "missing",
                            "prompt_tokens": None,
                            "completion_tokens": None,
                            "attempt_count": 0,
                        },
                    }
                ]
            ),
        )

        billing_event = next(
            event for event in RequestLogRecorder.build_events_from_summary(log)
            if event.envelope.event_name == "request_billing"
        )

        self.assertEqual(billing_event.payload["billing_stage"], "billed")
        self.assertEqual(billing_event.payload["billing_event_id"], "billing-final-1007")
        self.assertEqual(billing_event.payload["token_source"], "upstream_usage")
        self.assertEqual(billing_event.payload["prompt_tokens"], 12)
        self.assertEqual(billing_event.payload["completion_tokens"], 3)
        self.assertEqual(billing_event.payload["attempt_count"], 1)

    def test_request_status_resolution_covers_live_stream_and_final_states(self):
        self.assertEqual(
            LogService.resolve_request_status(
                request_status=None,
                success=False,
                status_code=LogService.STREAM_IN_PROGRESS_STATUS_CODE,
                is_stream=False,
            ),
            LogService.REQUEST_STATUS_REQUESTING,
        )
        self.assertEqual(
            LogService.resolve_request_status(
                request_status=None,
                success=True,
                status_code=LogService.STREAM_IN_PROGRESS_STATUS_CODE,
                is_stream=True,
            ),
            LogService.REQUEST_STATUS_RESPONDING,
        )
        self.assertEqual(
            LogService.resolve_request_status(
                request_status=None,
                success=False,
                status_code=500,
                is_stream=False,
            ),
            LogService.REQUEST_STATUS_COMPLETED,
        )

    def test_stream_placeholder_log_can_be_updated_to_final_state(self):
        log = RequestLog(
            id=1003,
            log_type="claude_messages",
            trace_id="trace-stream-placeholder",
            request_id="req-stream-placeholder",
            request_path="/v1/messages",
            http_method="POST",
            is_stream=True,
            success=True,
            billable=False,
            status_code=LogService.STREAM_IN_PROGRESS_STATUS_CODE,
            message="streaming response in progress",
        )

        LogService._apply_log_update_payload(
            log,
            {
                "log_type": "claude_messages",
                "trace_id": "trace-stream-placeholder",
                "request_id": "req-stream-placeholder",
                "request_path": "/v1/messages",
                "http_method": "POST",
                "is_stream": True,
                "success": True,
                "billable": False,
                "status_code": 200,
                "duration_ms": 128,
                "first_token_latency_ms": 20,
                "ttfb_ms": 20,
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "total_tokens": 8,
                "message": "stream claude_messages success",
                "trace": [{"result": "finished"}],
            },
        )

        self.assertEqual(log.status_code, 200)
        self.assertTrue(log.success)
        self.assertEqual(log.message, "stream claude_messages success")
        self.assertEqual(log.duration_ms, 128)
        self.assertEqual(log.ttfb_ms, 20)
        self.assertEqual(log.total_tokens, 8)
        self.assertIn("finished", log.trace_json or "")

    def test_upsert_log_updates_existing_main_row_for_same_external_request(self):
        existing = RequestLog(
            id=1006,
            log_type="chat",
            trace_id="trace-one-main-row",
            request_id="req-one-main-row",
            request_path="/v1/chat/completions",
            http_method="POST",
            is_stream=True,
            success=False,
            billable=False,
            status_code=LogService.STREAM_IN_PROGRESS_STATUS_CODE,
            request_status=LogService.REQUEST_STATUS_REQUESTING,
            message="正在请求提供商",
        )
        fake_db = SimpleNamespace(
            flush=lambda: None,
            commit=lambda: None,
            refresh=lambda item: None,
        )

        with (
            patch.object(LogService, "_find_log_by_identity", return_value=existing),
            patch.object(LogService, "create_log") as create_log,
            patch.object(LogService, "enqueue_finalize_for_log"),
            patch("app.logging.adapters.request_adapter.RequestLogRecorder.record_missing_events_from_summary", return_value=0),
            patch("app.services.ip_management_event_service.IpManagementEventService.attach_request_context"),
        ):
            updated = LogService.upsert_log(
                fake_db,
                log_type="chat",
                trace_id="trace-one-main-row",
                request_id="req-one-main-row",
                request_path="/v1/chat/completions",
                http_method="POST",
                is_stream=True,
                success=True,
                billable=False,
                status_code=LogService.STREAM_IN_PROGRESS_STATUS_CODE,
                request_status=LogService.REQUEST_STATUS_RESPONDING,
                message="响应中",
                trace=[{"result": "stream_opened"}],
                enqueue_finalize=False,
                record_runtime_signals=False,
            )

        self.assertIs(updated, existing)
        create_log.assert_not_called()
        self.assertEqual(existing.request_status, LogService.REQUEST_STATUS_RESPONDING)
        self.assertEqual(existing.message, "响应中")
        self.assertIn("stream_opened", existing.trace_json or "")

    def test_request_log_queue_updates_existing_row_and_finalizes_exact_usage(self):
        existing = RequestLog(
            id=1007,
            log_type="chat",
            trace_id="trace-queue-final",
            request_id="req-queue-final",
            request_path="/v1/chat/completions",
            http_method="POST",
            is_stream=True,
            success=True,
            billable=False,
            status_code=LogService.STREAM_IN_PROGRESS_STATUS_CODE,
            request_status=LogService.REQUEST_STATUS_RESPONDING,
            duration_ms=120,
            ttfb_ms=20,
            message="响应中",
        )
        created = RequestLog(
            id=1008,
            log_type="chat",
            trace_id="trace-queue-new",
            request_id="req-queue-new",
            request_path="/v1/chat/completions",
            http_method="POST",
            is_stream=False,
            success=True,
            billable=True,
            status_code=200,
        )
        existing_signature = RequestLogQueueService._dedupe_signature(
            {
                "trace_id": existing.trace_id,
                "request_id": existing.request_id,
                "log_type": existing.log_type,
                "request_path": existing.request_path,
                "http_method": existing.http_method,
            }
        )
        fake_db = SimpleNamespace(
            flush=lambda: None,
            commit=lambda: None,
            rollback=lambda: None,
            close=lambda: None,
        )

        with (
            patch("app.services.request_log_queue_service.SessionLocal", return_value=fake_db),
            patch.object(RequestLogQueueService, "_load_existing_logs_for_payloads", return_value={existing_signature: existing}),
            patch.object(RequestLogQueueService, "_load_valid_provider_ids", return_value=set()),
            patch.object(LogService, "create_log", return_value=created) as create_log,
            patch.object(LogService, "finalize_billing_if_exact_usage") as finalize_billing,
            patch.object(RequestLogQueueService, "_enqueue_finalize_jobs") as enqueue_finalize_jobs,
            patch("app.logging.adapters.request_adapter.RequestLogRecorder.record_events_from_summary") as record_events,
            patch("app.logging.adapters.request_adapter.RequestLogRecorder.record_missing_events_from_summary") as record_missing_events,
            patch("app.services.ip_management_event_service.IpManagementEventService.attach_request_context"),
        ):
            RequestLogQueueService._write_payloads_batch(
                [
                    {
                        "log_type": "chat",
                        "trace_id": existing.trace_id,
                        "request_id": existing.request_id,
                        "request_path": existing.request_path,
                        "http_method": "POST",
                        "is_stream": True,
                        "success": True,
                        "billable": True,
                        "status_code": 200,
                        "duration_ms": 4571,
                        "first_token_latency_ms": 159,
                        "ttfb_ms": 159,
                        "prompt_tokens": 5,
                        "completion_tokens": 292,
                        "total_tokens": 297,
                        "message": "stream chat success",
                        "trace": [{"result": "finished"}],
                    },
                    {
                        "log_type": "chat",
                        "trace_id": created.trace_id,
                        "request_id": created.request_id,
                        "request_path": created.request_path,
                        "http_method": "POST",
                        "success": True,
                        "status_code": 200,
                    },
                ]
            )

        create_log.assert_called_once()
        self.assertEqual(existing.status_code, 200)
        self.assertEqual(existing.duration_ms, 4571)
        self.assertEqual(existing.ttfb_ms, 159)
        self.assertEqual(existing.total_tokens, 297)
        self.assertEqual(existing.tps, round((292 * 1000) / (4571 - 159), 4))
        self.assertIn("finished", existing.trace_json or "")
        self.assertEqual(finalize_billing.call_count, 2)
        record_events.assert_called_once_with(fake_db, created, auto_commit=False)
        record_missing_events.assert_called_once_with(fake_db, existing, auto_commit=False)
        enqueue_finalize_jobs.assert_called_once()

    def test_record_missing_events_deduplicates_provider_attempt_rows(self):
        log = RequestLog(
            id=1004,
            log_type="responses",
            trace_id="trace-dedupe-provider-attempt",
            request_path="/v1/responses",
            requested_model="gpt-5.4",
            model_name="gpt-5.4",
            success=False,
            trace_json=dumps_json(
                [
                    {
                        "typed_event": "request_provider_attempt",
                        "result": "http_error",
                        "payload": {
                            "attempt_index": 1,
                            "provider_id": 12,
                            "provider_name": "测试提供商",
                            "provider_model_id": 34,
                            "actual_model": "gpt-5.4",
                            "endpoint_path": "/v1/responses",
                            "protocol_type": "openai",
                            "route_round": 1,
                            "retry_index": 0,
                            "capacity_lease_acquired": True,
                            "result": "http_error",
                            "status_code": 500,
                            "latency_ms": 120,
                            "upstream_request_id": "up-1",
                            "error_code": "upstream_error",
                            "error_message": "temporary failure",
                            "retryable": True,
                            "retry_wait_seconds": 1.5,
                            "retry_wait_plan_json": dumps_json({"sleep_seconds": 1.5}),
                            "detail_json": dumps_json({"reason": "temporary"}),
                        },
                    },
                    {
                        "typed_event": "request_provider_attempt",
                        "result": "http_error",
                        "payload": {
                            "attempt_index": 2,
                            "provider_id": 12,
                            "provider_name": "测试提供商",
                            "provider_model_id": 35,
                            "actual_model": "gpt-5.4",
                            "endpoint_path": "/v1/responses",
                            "protocol_type": "openai",
                            "route_round": 1,
                            "retry_index": 1,
                            "capacity_lease_acquired": True,
                            "result": "http_error",
                            "status_code": 502,
                            "latency_ms": 240,
                            "upstream_request_id": "up-2",
                            "error_code": "bad_gateway",
                            "error_message": "upstream bad gateway",
                            "retryable": True,
                            "retry_wait_seconds": 2.0,
                            "retry_wait_plan_json": dumps_json({"sleep_seconds": 2.0}),
                            "detail_json": dumps_json({"reason": "retry"}),
                        },
                    },
                ]
            ),
        )

        existing = RequestProviderAttemptEvent(
            request_log_id=log.id,
            trace_id=log.trace_id,
            attempt_index=1,
            provider_id=12,
            provider_name="测试提供商",
            provider_model_id=34,
            actual_model="gpt-5.4",
            endpoint_path="/v1/responses",
            protocol_type="openai",
            route_round=1,
            retry_index=0,
            capacity_lease_acquired=True,
            result="http_error",
            status_code=500,
            latency_ms=120,
            upstream_request_id="up-1",
            error_code="upstream_error",
            error_message="temporary failure",
            retryable=True,
            retry_wait_seconds=1.5,
            retry_wait_plan_json=dumps_json({"sleep_seconds": 1.5}),
            detail_json=dumps_json({"reason": "temporary"}),
        )

        def scalars(stmt):
            entity = stmt.column_descriptions[0].get("entity") if getattr(stmt, "column_descriptions", None) else None
            rows = [existing] if entity is RequestProviderAttemptEvent else []
            return SimpleNamespace(all=lambda: rows)

        fake_db = SimpleNamespace(scalars=scalars)
        with patch("app.logging.adapters.request_adapter.LoggingDispatcher.record_many") as record_many:
            count = RequestLogRecorder.record_missing_events_from_summary(fake_db, log)

        self.assertEqual(count, 3)
        self.assertEqual(record_many.call_count, 1)
        [events], _ = record_many.call_args
        provider_events = [
            event for event in events if event.envelope.event_name == "request_provider_attempt"
        ]
        self.assertEqual(len(provider_events), 1)
        self.assertEqual(provider_events[0].payload["attempt_index"], 2)
        self.assertEqual(provider_events[0].payload["retry_index"], 1)

    def test_route_exhausted_wait_trace_emits_retry_guard_fields(self):
        trace: list[dict] = []
        setting = SimpleNamespace()

        ProxyService._append_route_exhausted_retry_trace(
            trace,
            reason="route_outage",
            sleep_seconds=3.5,
            started_at=0.0,
            retry_round=1,
            setting=setting,
            diagnostics={"summary": {"reason": "route_outage"}, "reason_counts": {"route_outage": 2}},
            upstream_error={"status_code": 503, "detail": {"code": "upstream_busy"}},
            retry_wait_plan={"sleep_seconds": 3.5, "wait_source": "route_outage"},
        )

        decision_payload = next(
            item["payload"]
            for item in trace
            if item.get("typed_event") == "request_route_decision"
        )
        self.assertEqual(decision_payload["route_round"], 2)
        self.assertEqual(decision_payload["selection_guard"], "route_exhausted_wait_retry")
        self.assertEqual(decision_payload["retry_wait_ms"], 3500)
        retry_wait_plan = safeJsonParse(decision_payload["retry_wait_plan_json"])
        self.assertEqual(retry_wait_plan["wait_source"], "route_outage")

    def test_timeline_payload_includes_full_original_response_fields(self):
        response_json = dumps_json({
            "id": "chatcmpl-full-response",
            "choices": [{"message": {"role": "assistant", "content": "完整响应"}}],
        })
        log = RequestLog(
            id=1005,
            log_type="chat",
            trace_id="trace-full-response",
            request_id="req-full-response",
            request_path="/v1/chat/completions",
            http_method="POST",
            is_stream=False,
            has_image=False,
            request_status=LogService.REQUEST_STATUS_COMPLETED,
            success=True,
            billable=True,
            status_code=200,
            billing_attempt_count=0,
            token_finalize_attempt_count=0,
            response_body_json=response_json,
            response_text="完整响应",
            created_at=datetime(2026, 6, 18, 8, 0, 0),
        )
        fake_db = SimpleNamespace(
            scalars=lambda stmt: SimpleNamespace(all=lambda: []),
        )

        payload = build_request_log_timeline_payload(fake_db, log)

        self.assertEqual(payload["request_log"]["response_body_json"], response_json)
        self.assertEqual(payload["request_log"]["response_text"], "完整响应")

    def test_response_payload_logging_preserves_full_json_when_payload_logging_disabled(self):
        setting = SimpleNamespace(
            enable_payload_logging=False,
            mask_sensitive_fields=True,
            max_logged_body_bytes=32,
        )
        response_payload = {
            "id": "chatcmpl-full-response",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "这是一段明显超过普通日志截断阈值的完整响应内容。",
                    }
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
            "metadata": {"access_token": "should-not-leak"},
        }

        serialized = ProxyService._serialize_response_payload_for_logging(response_payload, setting=setting)
        parsed = safeJsonParse(serialized)

        self.assertEqual(parsed["id"], response_payload["id"])
        self.assertEqual(parsed["choices"], response_payload["choices"])
        self.assertEqual(parsed["usage"], response_payload["usage"])
        self.assertEqual(parsed["metadata"]["access_token"], "***")
        self.assertEqual(parsed["usage"]["total_tokens"], 46)
        self.assertNotIn("_truncated", parsed)
        self.assertNotIn("preview", parsed)


if __name__ == "__main__":
    unittest.main()
