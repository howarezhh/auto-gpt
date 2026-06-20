from __future__ import annotations

from enum import Enum


class RoutePipelineStrategy(str, Enum):
    AVAILABILITY_FIRST = "availability_first"
    COST_FIRST = "cost_first"
    LATENCY_FIRST = "latency_first"
    CAPACITY_AVOIDANCE = "capacity_avoidance"
    BALANCED = "balanced"


class RouteHealthGateMode(str, Enum):
    PERMISSIVE = "permissive"
    HEALTHY_ONLY = "healthy_only"
    TRUSTED_HEALTHY = "trusted_healthy"


class RouteStage(str, Enum):
    CONTEXT = "context"
    FILTERS = "filters"
    SCORER = "scorer"
    ORDERER = "orderer"
    DIAGNOSER = "diagnoser"


class RouteStageResult(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class RouteCandidateAction(str, Enum):
    KEPT = "kept"
    REJECTED = "rejected"
    SCORED = "scored"
    ORDERED = "ordered"
    SELECTED = "selected"


class RouteRequestKind(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"
    COMPLETIONS = "completions"
    EMBEDDINGS = "embeddings"
    MODERATIONS = "moderations"
    IMAGE_GENERATIONS = "image_generations"
    IMAGE_EDITS = "image_edits"
    IMAGE_VARIATIONS = "image_variations"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"
    GEMINI_STREAM_GENERATE_CONTENT = "gemini_stream_generate_content"
    CLAUDE_MESSAGES = "claude_messages"
    MODELS = "models"
    GENERIC = "generic"


class RouteFilterId(str, Enum):
    PROVIDER_ENABLED = "provider_enabled"
    PROVIDER_AUTHORIZED = "provider_authorized"
    ENDPOINT_PROTOCOL = "endpoint_protocol"
    MODEL_ENABLED = "model_enabled"
    MODEL_NAME_MATCH = "model_name_match"
    HEALTH_GATE = "health_gate"
    CONTENT_TRUST = "content_trust"
    CAPABILITY = "capability"
    CAPACITY = "capacity"
    CIRCUIT_BREAKER = "circuit_breaker"
