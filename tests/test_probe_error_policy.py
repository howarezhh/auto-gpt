from app.services.probe_error_policy_service import ProbeErrorPolicyService


def test_probe_error_policy_classifies_upstream_error_types():
    cases = [
        ({"status_code": 401, "message": "invalid api key"}, "upstream_auth_error"),
        ({"status_code": 429, "message": "rate_limit_exceeded"}, "upstream_rate_limited"),
        ({"status_code": 504, "message": "service unavailable"}, "upstream_5xx_unavailable"),
        ({"status_code": 408, "message": "request timeout"}, "upstream_timeout"),
        ({"status_code": 404, "message": "unknown endpoint"}, "endpoint_explicit_unsupported"),
        ({"status_code": 400, "message": "model_not_found"}, "model_or_capability_not_supported"),
        ({"status_code": None, "message": "incorrect header check while decompressing gzip"}, "transport_decompression_error"),
        ({"status_code": None, "message": "DNS connection reset"}, "upstream_network_error"),
        ({"status_code": None, "message": "insufficient balance"}, "upstream_quota_or_billing"),
    ]

    for payload, expected_code in cases:
        policy = ProbeErrorPolicyService.classify(**payload)
        assert policy.error_code == expected_code
        assert policy.frontend_label
        assert policy.handling_strategy


def test_probe_error_policy_classifies_probe_control_and_content_guard():
    rate_limited = ProbeErrorPolicyService.classify_result(
        {
            "success": False,
            "support_mode": "probe_rate_limited",
            "status_code": 429,
            "message": "探针已限频",
        }
    )
    assert rate_limited["error_code"] == "probe_rate_limited"
    assert rate_limited["update_allowed"] is False
    assert rate_limited["state_update_policy"] == "skip_all_state_updates"

    content_risk = ProbeErrorPolicyService.classify_result(
        {
            "success": False,
            "support_mode": "content_guard_failed",
            "status_code": 200,
            "message": "固定答案不一致",
            "content_guard": {
                "content_guard_categories_json": '["fixed_answer_probe_mismatch"]',
                "content_guard_reason": "固定答案不一致",
            },
        },
        probe_kind="content_guard",
    )
    assert content_risk["error_code"] == "content_integrity_violation"
    assert content_risk["update_allowed"] is True
    assert content_risk["state_update_policy"] == "content_guard_may_update_trust_state"


def test_probe_error_policy_annotates_result_with_log_fields():
    result = {
        "success": False,
        "status_code": 503,
        "message": "temporarily unavailable",
        "support_mode": "unknown",
    }

    annotated = ProbeErrorPolicyService.annotate_result(result)

    assert annotated is result
    assert annotated["error_code"] == "upstream_5xx_unavailable"
    assert annotated["frontend_label"] == "上游故障"
    assert annotated["retryable"] is True
    assert annotated["update_allowed"] is False
    assert annotated["log_fields"]["error_code"] == "upstream_5xx_unavailable"
