from types import SimpleNamespace

from app.routers.logging_api import _enrich_health_run_probe_context


class _FakeScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def scalars(self, _statement):
        return _FakeScalarResult(
            [
                SimpleNamespace(
                    run_id="run-1",
                    provider_id=23,
                    provider_model_id=101,
                    model_name="gpt-5.5",
                    created_at="2026-06-12T10:00:00",
                    id=1,
                ),
                SimpleNamespace(
                    run_id="run-1",
                    provider_id=24,
                    provider_model_id=102,
                    model_name="gpt-5.5-mini",
                    created_at="2026-06-12T10:00:01",
                    id=2,
                ),
                SimpleNamespace(
                    run_id="run-1",
                    provider_id=23,
                    provider_model_id=101,
                    model_name="gpt-5.5",
                    created_at="2026-06-12T10:00:02",
                    id=3,
                ),
                SimpleNamespace(
                    run_id="run-1",
                    provider_id=25,
                    provider_model_id=None,
                    model_name="备用模型",
                    created_at="2026-06-12T10:00:03",
                    id=4,
                ),
            ]
        )

    def execute(self, _statement):
        return _FakeScalarResult([(23, "聚合"), (24, "备用提供商")])


def test_health_run_list_enriches_probe_provider_names_and_model_ids():
    items = [{"run_id": "run-1"}]

    _enrich_health_run_probe_context(_FakeDb(), items)

    assert items[0]["health_probe_provider_names"] == ["聚合", "备用提供商", "提供商 25"]
    assert items[0]["health_probe_model_ids"] == ["101", "102", "未记录 ID：备用模型"]
    assert items[0]["health_probe_provider_count"] == 3
    assert items[0]["health_probe_model_count"] == 3
