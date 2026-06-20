from __future__ import annotations

import hashlib
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from app.services.cache_service import CacheService
from app.utils.json_utils import dumps_json


class FixedSuccessResponseService:
    """Detects repeated fixed-text HTTP 200 responses that are really upstream failures."""

    CACHE_PREFIX = "fixed-success-response"
    CACHE_TTL_SECONDS = 60 * 60
    SIMILARITY_THRESHOLD = 0.95
    MIN_NORMALIZED_CHARS = 6
    MAX_NORMALIZED_CHARS = 1200
    EXCERPT_CHARS = 160

    @classmethod
    def inspect_and_record(
        cls,
        *,
        provider_id: int,
        provider_model_id: int,
        request_payload: Any,
        response_text: str | None,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        normalized = cls.normalize_response_text(response_text)
        request_fingerprint = cls.request_fingerprint(request_payload)
        response_fingerprint = cls.text_fingerprint(normalized)
        sample = {
            "request_fingerprint": request_fingerprint,
            "response_fingerprint": response_fingerprint,
            "normalized_text": normalized,
            "normalized_text_excerpt": cls.excerpt(normalized),
            "normalized_length": len(normalized),
        }
        key = cls.cache_key(provider_id, provider_model_id)
        previous_samples = cls._load_samples(key)
        comparison_sample = previous_samples[-1] if previous_samples else None
        similarity = cls.similarity(
            normalized,
            str(comparison_sample.get("normalized_text") or comparison_sample.get("normalized_text_excerpt") or "") if comparison_sample else "",
        )
        used_threshold = float(threshold or cls.SIMILARITY_THRESHOLD)
        detected = bool(
            comparison_sample
            and cls._eligible_text(normalized)
            and cls._eligible_text(str(comparison_sample.get("normalized_text") or comparison_sample.get("normalized_text_excerpt") or ""))
            and request_fingerprint != comparison_sample.get("request_fingerprint")
            and similarity >= used_threshold
        )
        samples = (previous_samples + [sample])[-2:]
        cls._store_samples(key, samples)
        return {
            "detected": detected,
            "similarity": round(similarity, 6),
            "threshold": used_threshold,
            "request_fingerprint": request_fingerprint,
            "current_response_fingerprint": response_fingerprint,
            "previous_request_fingerprint": comparison_sample.get("request_fingerprint") if comparison_sample else None,
            "previous_response_fingerprint": comparison_sample.get("response_fingerprint") if comparison_sample else None,
            "normalized_text_excerpt": sample["normalized_text_excerpt"],
            "previous_normalized_text_excerpt": comparison_sample.get("normalized_text_excerpt") if comparison_sample else None,
            "normalized_length": sample["normalized_length"],
        }

    @classmethod
    def normalize_response_text(cls, value: str | None) -> str:
        text = unicodedata.normalize("NFKC", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip().lower()
        return text[: cls.MAX_NORMALIZED_CHARS]

    @staticmethod
    def request_fingerprint(value: Any) -> str:
        try:
            raw = dumps_json(value, sort_keys=True, separators=(",", ":"))
        except TypeError:
            raw = str(value)
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]

    @staticmethod
    def text_fingerprint(value: str) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8", errors="ignore")).hexdigest()[:24]

    @classmethod
    def excerpt(cls, value: str | None) -> str:
        return cls.normalize_response_text(value)[: cls.EXCERPT_CHARS]

    @staticmethod
    def similarity(left: str | None, right: str | None) -> float:
        left_text = str(left or "")
        right_text = str(right or "")
        if not left_text or not right_text:
            return 0.0
        if left_text == right_text:
            return 1.0
        return SequenceMatcher(None, left_text, right_text).ratio()

    @classmethod
    def cache_key(cls, provider_id: int, provider_model_id: int) -> str:
        return f"{cls.CACHE_PREFIX}:{int(provider_id)}:{int(provider_model_id)}"

    @classmethod
    def clear_samples(cls, provider_id: int, provider_model_id: int) -> None:
        CacheService.invalidate(cls.cache_key(provider_id, provider_model_id))

    @classmethod
    def _eligible_text(cls, value: str) -> bool:
        length = len(str(value or ""))
        return cls.MIN_NORMALIZED_CHARS <= length <= cls.MAX_NORMALIZED_CHARS

    @staticmethod
    def _load_samples(key: str) -> list[dict[str, Any]]:
        cached = CacheService.get(key)
        if not isinstance(cached, list):
            return []
        return [item for item in cached if isinstance(item, dict)][-2:]

    @classmethod
    def _store_samples(cls, key: str, samples: list[dict[str, Any]]) -> None:
        CacheService.set(key, samples[-2:], ttl_seconds=cls.CACHE_TTL_SECONDS)
