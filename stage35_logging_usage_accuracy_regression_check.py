from __future__ import annotations

from decimal import Decimal
import os
from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.api_client_key import ApiClientKey
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.services.billing_service import BillingService
from app.services.log_service import LogService
from app.services.proxy_service import ProxyService
from app.services.token_usage_service import TokenUsageService


def main() -> None:
    database_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://aotu_gpt:zhh123456@127.0.0.1:5432/aotu_gpt_test",
    )
    engine = create_engine(database_url, future=True)
    if True:
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = SessionLocal()
        try:
            provider = Provider(
                name="官方usage测试提供商",
                base_url="https://api.openai.com/v1",
                api_key="sk-test",
                provider_type="openai_compatible",
            )
            db.add(provider)
            db.flush()
            provider_model = ProviderModel(
                provider_id=provider.id,
                model_name="gpt-5-usage-test",
                input_price_per_1k=Decimal("0.010000"),
                output_price_per_1k=Decimal("0.030000"),
                cache_price_per_1k=Decimal("0.002000"),
                price_multiplier=Decimal("1.0"),
            )
            db.add(provider_model)
            user = UserAccount(
                username="stage35-usage-user",
                email="stage35-usage-user@example.com",
                password_hash="stage35",
                balance_amount=Decimal("10.0"),
                total_recharge_amount=Decimal("10.0"),
            )
            db.add(user)
            db.flush()
            api_key = ApiClientKey(
                name="测试日志密钥",
                key_prefix="sk-test",
                key_hash="stage35-hash",
                enabled=True,
                owner_user_id=user.id,
            )
            db.add(api_key)
            db.commit()

            chat_response = {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "prompt_tokens_details": {
                        "cached_tokens": 40,
                        "audio_tokens": 3,
                    },
                    "completion_tokens_details": {
                        "reasoning_tokens": 11,
                        "audio_tokens": 2,
                        "accepted_prediction_tokens": 7,
                        "rejected_prediction_tokens": 5,
                    },
                }
            }
            chat_usage = ProxyService._extract_usage_info(chat_response)
            assert chat_usage == {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cache_read_tokens": 40,
                "cache_write_tokens": None,
            }
            chat_log = LogService.create_log(
                db,
                log_type="chat",
                provider_id=provider.id,
                provider_name=provider.name,
                resolved_provider_model_id=provider_model.id,
                request_path="/v1/chat/completions",
                http_method="POST",
                requested_model="gpt-5-usage-test",
                model_name="gpt-5-usage-test",
                success=True,
                status_code=200,
                latency_ms=200,
                duration_ms=200,
                first_token_latency_ms=40,
                prompt_tokens=chat_usage["prompt_tokens"],
                completion_tokens=chat_usage["completion_tokens"],
                total_tokens=chat_usage["total_tokens"],
                cache_read_tokens=chat_usage["cache_read_tokens"],
                cache_write_tokens=chat_usage["cache_write_tokens"],
                api_client_key_id=api_key.id,
                token_response_payload=chat_response,
                schedule_token_fill=False,
                enqueue_finalize=False,
            )
            assert chat_log.token_source == "upstream_usage"
            assert chat_log.upstream_usage_missing is False
            assert chat_log.cache_read_tokens == 40
            assert chat_log.cache_write_tokens is None
            assert chat_log.reasoning_tokens == 11
            assert chat_log.prompt_audio_tokens == 3
            assert chat_log.completion_audio_tokens == 2
            assert chat_log.accepted_prediction_tokens == 7
            assert chat_log.rejected_prediction_tokens == 5
            assert chat_log.ttfb_ms == 40
            assert chat_log.duration_ms == 200
            assert chat_log.tps == round((50 * 1000) / (200 - 40), 4)
            assert LogService.is_token_billing_finalize_candidate(chat_log)

            failed_chat_log = LogService.create_log(
                db,
                log_type="chat",
                provider_id=provider.id,
                provider_name=provider.name,
                resolved_provider_model_id=provider_model.id,
                request_path="/v1/chat/completions",
                http_method="POST",
                requested_model="gpt-5-usage-test",
                model_name="gpt-5-usage-test",
                success=False,
                status_code=503,
                api_client_key_id=api_key.id,
                schedule_token_fill=False,
                enqueue_finalize=False,
            )
            assert not LogService.is_token_billing_finalize_candidate(failed_chat_log)
            assert failed_chat_log.billing_status == "no_charge"
            assert failed_chat_log.billing_finalized_at is not None

            file_log = LogService.create_log(
                db,
                log_type="files",
                provider_id=provider.id,
                provider_name=provider.name,
                request_path="/v1/files",
                http_method="GET",
                success=True,
                status_code=200,
                api_client_key_id=api_key.id,
                schedule_token_fill=False,
                enqueue_finalize=False,
            )
            assert not LogService.is_token_billing_finalize_candidate(file_log)
            assert file_log.billing_status == "no_charge"
            assert file_log.billing_finalized_at is not None
            for log_type, request_path in (
                ("moderations", "/v1/moderations"),
                ("images", "/v1/images/generations"),
                ("v1_preflight", "/v1/chat/completions"),
                ("unsupported_endpoint", "/v1/unsupported"),
            ):
                non_billing_log = LogService.create_log(
                    db,
                    log_type=log_type,
                    provider_id=provider.id,
                    provider_name=provider.name,
                    request_path=request_path,
                    http_method="POST",
                    success=True,
                    status_code=200,
                    api_client_key_id=api_key.id,
                    schedule_token_fill=False,
                    enqueue_finalize=False,
                )
                assert not LogService.is_token_billing_finalize_candidate(non_billing_log)
                assert non_billing_log.billing_status == "no_charge"
                assert non_billing_log.billing_finalized_at is not None
            pending_finalize_count = db.scalar(
                select(func.count()).select_from(RequestLog).where(
                    LogService._token_billing_finalize_candidate_expr(),
                    RequestLog.billing_finalized_at.is_(None),
                )
            )
            assert pending_finalize_count == 1

            billing_data = BillingService.compute_log_cost(db, chat_log)
            assert billing_data["billing_status"] == "billed"
            assert billing_data["prompt_cost"] == Decimal("0.000680")
            assert billing_data["completion_cost"] == Decimal("0.001500")
            assert billing_data["total_cost"] == Decimal("0.002180")

            responses_response = {
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 20,
                    "total_tokens": 100,
                    "input_tokens_details": {
                        "cached_tokens": 12,
                        "audio_tokens": 4,
                    },
                    "output_tokens_details": {
                        "reasoning_tokens": 6,
                        "audio_tokens": 1,
                    },
                }
            }
            responses_usage = TokenUsageService._extract_usage_from_response(responses_response)
            assert responses_usage["has_usage"] is True
            assert responses_usage["prompt_tokens"] == 80
            assert responses_usage["completion_tokens"] == 20
            assert responses_usage["total_tokens"] == 100
            assert responses_usage["cache_read_tokens"] == 12
            assert responses_usage["cache_write_tokens"] is None
            assert responses_usage["reasoning_tokens"] == 6
            assert responses_usage["prompt_audio_tokens"] == 4
            assert responses_usage["completion_audio_tokens"] == 1

            payload_only_log = LogService.create_log(
                db,
                log_type="responses",
                provider_id=provider.id,
                provider_name=provider.name,
                resolved_provider_model_id=provider_model.id,
                request_path="/v1/responses",
                http_method="POST",
                requested_model="gpt-5-usage-test",
                model_name="gpt-5-usage-test",
                success=True,
                status_code=200,
                latency_ms=180,
                token_response_payload=responses_response,
                schedule_token_fill=False,
                enqueue_finalize=False,
            )
            assert payload_only_log.prompt_tokens == 80
            assert payload_only_log.completion_tokens == 20
            assert payload_only_log.total_tokens == 100
            assert payload_only_log.cache_read_tokens == 12
            assert payload_only_log.token_source == "upstream_usage"
            assert payload_only_log.upstream_usage_missing is False

            estimated_log = LogService.create_log(
                db,
                log_type="responses",
                provider_id=provider.id,
                provider_name=provider.name,
                resolved_provider_model_id=provider_model.id,
                request_path="/v1/responses",
                http_method="POST",
                requested_model="gpt-5-usage-test",
                model_name="gpt-5-usage-test",
                success=True,
                status_code=200,
                latency_ms=120,
                prompt_tokens=8,
                completion_tokens=3,
                total_tokens=11,
                token_source="estimated",
                upstream_usage_missing=True,
                schedule_token_fill=False,
                enqueue_finalize=False,
            )
            TokenUsageService._fill_usage_for_log(
                db,
                estimated_log,
                response_payload=responses_response,
                enable_usage_fill=True,
            )
            assert estimated_log.prompt_tokens == 80
            assert estimated_log.completion_tokens == 20
            assert estimated_log.total_tokens == 100
            assert estimated_log.cache_read_tokens == 12
            assert estimated_log.token_source == "upstream_usage"
            assert estimated_log.upstream_usage_missing is False

            missing_usage = TokenUsageService._extract_usage_from_response({"choices": []})
            assert missing_usage["has_usage"] is False
            assert missing_usage["cache_read_tokens"] is None
            assert missing_usage["cache_write_tokens"] is None

            usage_info = {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
            }
            stream_chunk = (
                b'data: {"choices":[],"usage":{"prompt_tokens":9,'
                b'"completion_tokens":4,"total_tokens":13,'
                b'"prompt_tokens_details":{"cached_tokens":2}}}\n\n'
                b"data: [DONE]\n\n"
            )
            (
                response_text_bytes,
                token_response_bytes,
                finish_reason,
                collected_usage,
                usage_payload,
            ) = ProxyService._collect_stream_log_data(
                chunk=stream_chunk,
                event_buffer=bytearray(),
                response_text_parts=[],
                response_text_bytes=0,
                token_response_parts=[],
                token_response_bytes=0,
                finish_reason=None,
                usage_info=usage_info,
                usage_payload=None,
                generated_image_summary=None,
                capture_text=True,
                capture_usage=True,
                limit_bytes=1024,
                token_limit_bytes=1024,
                stream_log_fast_path="chat",
            )
            assert response_text_bytes == 0
            assert token_response_bytes == 0
            assert finish_reason is None
            assert collected_usage["prompt_tokens"] == 9
            assert collected_usage["completion_tokens"] == 4
            assert collected_usage["total_tokens"] == 13
            assert collected_usage["cache_read_tokens"] == 2
            assert usage_payload["prompt_tokens_details"]["cached_tokens"] == 2

            prepared = ProxyService._prepare_upstream_request(
                SimpleNamespace(base_url="https://api.openai.com/v1"),
                endpoint_path="/completions",
                payload={
                    "model": "gpt-5-usage-test",
                    "prompt": "你好",
                    "stream": True,
                    "__aotu_include_usage": True,
                },
            )
            assert prepared.request_path == "/chat/completions"
            assert prepared.adapt_chat_response_to_completions is True
            assert prepared.request_payload["stream_options"]["include_usage"] is True
            assert "__aotu_include_usage" not in prepared.request_payload
        finally:
            db.close()
            engine.dispose()
    print("stage35 logging usage accuracy regression check passed")


if __name__ == "__main__":
    main()
