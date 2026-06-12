import unittest
from types import SimpleNamespace

from app.logging.adapters.request_adapter import RequestLogRecorder
from app.models.request_log import RequestLog
from app.services.log_service import LogService
from app.services.proxy_service import ProxyService
from app.utils.json_utils import dumps_json


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


if __name__ == "__main__":
    unittest.main()
