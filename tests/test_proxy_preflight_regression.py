from decimal import Decimal
from types import SimpleNamespace

from app.services.proxy_service import ProxyService


class _FakeSession:
    def __init__(self, opened):
        self.opened = opened

    def __enter__(self):
        self.opened.append(True)
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def scalar(self, _statement):
        return None


def test_preflight_cost_estimation_opens_session_when_db_is_none(monkeypatch):
    opened_sessions = []

    monkeypatch.setattr("app.services.proxy_service.SessionLocal", lambda: _FakeSession(opened_sessions))
    monkeypatch.setattr(
        ProxyService,
        "_estimate_request_tokens_for_precheck",
        staticmethod(lambda *args, **kwargs: (10, None)),
    )

    provider_model = SimpleNamespace(
        model_name="测试模型",
        max_output_tokens=0,
        input_price_per_1k=Decimal("0.2"),
        output_price_per_1k=Decimal("0.4"),
        price_multiplier=Decimal("1"),
    )

    estimated_cost, input_tokens, output_tokens = ProxyService._estimate_preflight_request_cost(
        None,
        provider_model=provider_model,
        payload={"max_output_tokens": 5},
        request_path="/v1/responses",
        model_name="测试模型",
    )

    assert opened_sessions == [True]
    assert estimated_cost == Decimal("0.004")
    assert input_tokens == 10
    assert output_tokens == 5
