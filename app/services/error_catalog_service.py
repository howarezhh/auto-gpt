from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    code: str
    message: str
    status_code: int
    error_type: str
    category: str
    retryable: bool
    recoverable: bool
    next_action: str
    public: bool = True
    public_code: str | None = None
    public_message: str | None = None
    log_message: str | None = None
    handling_strategy: str | None = None
    alert_level: str = "none"


class ErrorCatalogService:
    """统一错误目录：同一错误码同时服务对外响应、日志、trace 和告警排障。"""

    CATEGORY_HANDLING_STRATEGIES: dict[str, str] = {
        "invalid_request": "核对请求路径、请求方法、JSON 结构、字段类型和大小限制；日志中仅保留结构摘要和必要字段。",
        "authentication": "核对 Authorization Bearer API Key、密钥哈希匹配、过期时间和来源 IP；禁止在日志中记录明文密钥。",
        "authorization": "核对 API Key、用户账号、模型、端点和提供商授权关系；记录授权对象 ID 与拒绝原因。",
        "quota": "核对账户可用余额、冻结金额和计费后台任务状态；余额恢复后允许调用方重试。",
        "rate_limit": "核对 API Key、账户、全局和提供商短窗口限流计数；等待窗口恢复或调整限流配置。",
        "capacity_limited": "核对全局、账户、API Key 和提供商活跃请求/流式请求/QPS/RPM 容量；检查 Redis 租约是否及时释放。",
        "model_unavailable": "核对模型名称、模型启用状态、API Key 授权、模型映射、挂载提供商和可用性检测结果。",
        "capability_not_supported": "核对请求端点、协议类型、模型能力、工具/视觉/图片生成能力和适配开关；避免把不可无损转换的请求投递到上游。",
        "route_unavailable": "核对候选筛选 trace、模型挂载、授权、可用状态、容量、熔断和协议能力，确认为什么没有可用候选。",
        "upstream_transient": "核对上游状态码、请求 ID、候选重试 trace、网络与提供商可用性；保留截断摘要，避免暴露原始敏感响应。",
        "network": "核对 Base URL、DNS、TLS、代理、防火墙、连接池和上游网络连通性。",
        "timeout": "核对连接、写入、首 Token、读取、空闲和最大持续时间配置；结合 trace 判断超时阶段。",
        "invalid_response": "核对上游 JSON/SSE/图片/文件响应结构；日志只记录结构摘要和缺失字段，禁止记录完整大字段或二进制内容。",
        "client_cancelled": "记录客户端断开时间、已完成阶段和资源释放结果；客户端取消不计为 provider 失败。",
        "resource_not_found": "核对资源 ID、归属账号、软删除状态、上游管理端点和本地缓存是否一致。",
        "conflict": "核对并发写入、唯一字段、任务状态和对象当前状态；按最新状态重新提交或先处理冲突对象。",
        "server_error": "按 trace_id 查询服务端日志、异常类型、后台任务状态、数据库/Redis 可用和最近配置变更；对外只返回安全摘要。",
        "cache": "核对缓存失效触发点、命中率、TTL、L1/L2 状态和相关写操作是否主动失效缓存。",
    }

    ALIAS_CODES: dict[str, str] = {
        "stream_required_for_long_output": "long_output_requires_stream",
    }

    SPECS: dict[str, ErrorSpec] = {
        "invalid_request": ErrorSpec(
            "invalid_request",
            "请求参数不合法，服务端无法按当前内容处理。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "检查请求路径、请求方法、JSON 字段和值类型后重新提交。",
        ),
        "request_validation_failed": ErrorSpec(
            "request_validation_failed",
            "请求参数校验失败，部分字段缺失或类型不符合接口要求。",
            422,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "按 detail.errors 中的字段位置修正请求参数。",
        ),
        "invalid_json_body": ErrorSpec(
            "invalid_json_body",
            "请求体不是合法的 JSON 对象，或请求体为空。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "提交 Content-Type 为 application/json 的非空 JSON 对象。",
        ),
        "request_body_too_large": ErrorSpec(
            "request_body_too_large",
            "请求体超过系统允许的大小上限，已在进入上游前拒绝。",
            413,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "压缩或拆分请求内容，或联系管理员调整请求体大小上限。",
        ),
        "request_tokens_exceeded": ErrorSpec(
            "request_tokens_exceeded",
            "请求 Token 数超过系统全局限制，已在路由前拒绝。",
            413,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "减少上下文、图片或工具参数内容后重试。",
        ),
        "model_input_tokens_exceeded": ErrorSpec(
            "model_input_tokens_exceeded",
            "请求输入 Token 数超过目标模型输入窗口。",
            413,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "减少输入上下文，或改用更大上下文窗口的模型。",
        ),
        "request_token_estimation_failed": ErrorSpec(
            "request_token_estimation_failed",
            "服务端无法安全估算请求 Token，已按最大 Token 策略拒绝。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "简化请求结构后重试，或联系管理员检查 tokenizer 配置。",
        ),
        "output_token_limit_exceeded": ErrorSpec(
            "output_token_limit_exceeded",
            "请求的最大输出 Token 超过系统或模型允许上限。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "降低 max_tokens、max_completion_tokens 或 max_output_tokens 后重试。",
        ),
        "stream_required_for_long_output": ErrorSpec(
            "stream_required_for_long_output",
            "请求的输出长度较大，当前接口要求使用流式响应。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "将请求参数 stream 设置为 true 后重新提交。",
        ),
        "invalid_image_file": ErrorSpec(
            "invalid_image_file",
            "上传的图片格式不受支持。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "请上传 PNG、JPEG、WEBP 或 GIF 图片。",
        ),
        "empty_image_file": ErrorSpec(
            "empty_image_file",
            "上传的图片文件为空。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "选择有效图片文件后重新上传。",
        ),
        "image_file_too_large": ErrorSpec(
            "image_file_too_large",
            "上传的图片超过系统大小上限。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "压缩图片到 10 MB 以内后重新上传。",
        ),
        "missing_model": ErrorSpec(
            "missing_model",
            "请求缺少 model 字段。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "在请求体中填写要调用的模型名称。",
        ),
        "model_not_found": ErrorSpec(
            "model_not_found",
            "请求的模型不存在，或当前 API Key 无权访问该模型。",
            404,
            "invalid_request_error",
            "model_unavailable",
            False,
            False,
            "确认模型名称是否正确，并检查模型授权、启用状态和路由策略。",
        ),
        "model_not_allowed": ErrorSpec(
            "model_not_allowed",
            "当前 API Key 未被授权调用该模型。",
            403,
            "invalid_request_error",
            "authorization",
            False,
            False,
            "在 API Key 授权范围中加入该模型，或改用已授权模型。",
        ),
        "capability_not_supported": ErrorSpec(
            "capability_not_supported",
            "当前模型或提供商不支持本次请求所需能力。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "改用支持该能力的模型或提供商，或移除不受支持的请求能力。",
        ),
        "unsupported_endpoint": ErrorSpec(
            "unsupported_endpoint",
            "当前 OpenAI 兼容端点不受支持。",
            404,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "改用响应 detail.supported_endpoints 中列出的标准端点。",
        ),
        "responses_endpoint_not_supported": ErrorSpec(
            "responses_endpoint_not_supported",
            "当前候选提供商或模型不支持原生 Responses API。",
            404,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "查看 detail.reason_details 和 detail.diagnostic_samples，改用支持 /v1/responses 的提供商或模型。",
        ),
        "chat_completions_endpoint_not_supported": ErrorSpec(
            "chat_completions_endpoint_not_supported",
            "当前候选提供商或模型不支持原生 Chat Completions API。",
            404,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "查看 detail.reason_details 和 detail.diagnostic_samples，改用支持 /v1/chat/completions 的提供商或模型。",
        ),
        "resource_not_found": ErrorSpec(
            "resource_not_found",
            "请求的资源不存在，或已被删除。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "刷新列表后重新选择可用对象。",
        ),
        "endpoint_not_allowed": ErrorSpec(
            "endpoint_not_allowed",
            "当前 API Key 未被授权访问该端点。",
            403,
            "authentication_error",
            "authorization",
            False,
            False,
            "调整 API Key 的端点授权范围，或改用已授权端点。",
        ),
        "endpoint_conversion_disabled": ErrorSpec(
            "endpoint_conversion_disabled",
            "端点协议自动转换已禁用，本次请求不能降级到其他协议端点。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "为请求端点配置原生支持的上游模型或显式启用兼容适配。",
        ),
        "endpoint_request_conversion_unsafe": ErrorSpec(
            "endpoint_request_conversion_unsafe",
            "请求包含无法安全转换到目标端点的字段。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "移除 detail.unsafe_fields 中的字段，或使用原生支持该协议的上游。",
        ),
        "endpoint_response_conversion_unsafe": ErrorSpec(
            "endpoint_response_conversion_unsafe",
            "上游响应无法安全转换为调用方请求的端点格式。",
            502,
            "server_error",
            "capability_not_supported",
            False,
            False,
            "使用原生支持该响应协议的提供商，避免跨端点响应转换。",
        ),
        "invalid_api_key": ErrorSpec(
            "invalid_api_key",
            "缺少或无效的 Bearer API Key，鉴权失败。",
            401,
            "authentication_error",
            "authentication",
            False,
            False,
            "在 Authorization 请求头中传入有效的 Bearer API Key。",
        ),
        "key_disabled": ErrorSpec(
            "key_disabled",
            "当前 API Key 已被停用。",
            403,
            "authentication_error",
            "authorization",
            False,
            False,
            "启用该 API Key，或更换其他可用 API Key。",
        ),
        "key_expired": ErrorSpec(
            "key_expired",
            "当前 API Key 已过期。",
            403,
            "authentication_error",
            "authorization",
            False,
            False,
            "延长 API Key 有效期，或创建新的 API Key。",
        ),
        "source_ip_not_allowed": ErrorSpec(
            "source_ip_not_allowed",
            "当前来源 IP 不在 API Key 允许范围内。",
            403,
            "authentication_error",
            "authorization",
            False,
            False,
            "将当前来源 IP 加入 API Key 白名单，或从允许的网络发起请求。",
        ),
        "owner_user_disabled": ErrorSpec(
            "owner_user_disabled",
            "API Key 所属用户账号已被停用。",
            403,
            "authentication_error",
            "authorization",
            False,
            False,
            "启用所属用户账号，或更换其他账号下的 API Key。",
        ),
        "no_authorized_provider": ErrorSpec(
            "no_authorized_provider",
            "当前 API Key 没有任何已授权提供商。",
            403,
            "authentication_error",
            "authorization",
            False,
            False,
            "至少为该 API Key 授权一个可用提供商。",
        ),
        "insufficient_balance": ErrorSpec(
            "insufficient_balance",
            "账户或 API Key 可用余额不足，请求已被拦截。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "充值或释放冻结金额后重试。",
        ),
        "rate_limit_exceeded": ErrorSpec(
            "rate_limit_exceeded",
            "请求触发限流，当前窗口内调用过于频繁。",
            429,
            "rate_limit_error",
            "rate_limit",
            True,
            True,
            "降低请求频率，等待限流窗口恢复后重试。",
        ),
        "concurrency_limit_exceeded": ErrorSpec(
            "concurrency_limit_exceeded",
            "当前并发请求数已达到限制。",
            429,
            "rate_limit_error",
            "capacity_limited",
            True,
            True,
            "等待正在执行的请求结束后重试，或调整并发上限。",
        ),
        "provider_capacity_exceeded": ErrorSpec(
            "provider_capacity_exceeded",
            "目标提供商当前容量已满。",
            429,
            "rate_limit_error",
            "capacity_limited",
            True,
            True,
            "等待容量释放后重试，或启用其他可用提供商参与路由。",
        ),
        "provider_capacity_unavailable": ErrorSpec(
            "provider_capacity_unavailable",
            "提供商容量状态暂时不可用，无法安全分配请求。",
            503,
            "server_error",
            "capacity_limited",
            True,
            True,
            "检查 Redis 或容量状态服务，恢复后重试。",
        ),
        "redis_unavailable": ErrorSpec(
            "redis_unavailable",
            "Redis 依赖不可用，限流或并发状态无法读取。",
            503,
            "server_error",
            "network",
            True,
            True,
            "恢复 Redis 连接后重试。",
            public=False,
            public_code="internal_server_error",
            public_message="系统依赖暂时不可用，请稍后重试。",
            log_message="Redis 依赖不可用，限流、并发、缓存或队列状态无法可靠读取。",
            handling_strategy="检查 REDIS_URL、Redis 连接、认证权限、网络连通性、连接池耗尽和降级策略是否符合生产规范。",
            alert_level="critical",
        ),
        "route_unavailable": ErrorSpec(
            "route_unavailable",
            "当前路由策略下没有可用候选提供商或模型。",
            503,
            "server_error",
            "route_unavailable",
            True,
            True,
            "检查模型挂载、可用状态、容量限制、熔断状态和 API Key 授权。",
        ),
        "all_providers_failed": ErrorSpec(
            "all_providers_failed",
            "所有候选提供商都请求失败，已返回最后一次失败原因。",
            502,
            "server_error",
            "upstream_transient",
            True,
            True,
            "查看 detail.last_error 和 trace，修复上游或等待上游恢复后重试。",
        ),
        "all_providers_unavailable_after_retry": ErrorSpec(
            "all_providers_unavailable_after_retry",
            "所有可用提供商在等待重试窗口内仍不可用，已停止内部重试。",
            503,
            "server_error",
            "route_unavailable",
            True,
            True,
            "查看 trace 中的候选排除原因，恢复容量、可用状态或授权后重试。",
        ),
        "upstream_request_failed": ErrorSpec(
            "upstream_request_failed",
            "上游服务返回失败，代理已保留上游错误信息。",
            502,
            "server_error",
            "upstream_transient",
            True,
            True,
            "根据 detail 中的上游状态码和错误内容处理，必要时稍后重试。",
        ),
        "upstream_connect_error": ErrorSpec(
            "upstream_connect_error",
            "无法连接上游服务。",
            502,
            "server_error",
            "network",
            True,
            True,
            "检查上游 Base URL、网络、防火墙、DNS 和代理配置后重试。",
        ),
        "upstream_read_timeout": ErrorSpec(
            "upstream_read_timeout",
            "读取上游响应超时。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "稍后重试，或调大超时时间并检查上游响应速度。",
        ),
        "request_timeout": ErrorSpec(
            "request_timeout",
            "请求处理超时。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "稍后重试，或缩短请求内容并检查上游延迟。",
        ),
        "non_stream_response_too_large": ErrorSpec(
            "non_stream_response_too_large",
            "上游非流式响应体超过系统大小上限。",
            502,
            "server_error",
            "invalid_response",
            False,
            False,
            "改用流式请求，或降低输出长度。",
        ),
        "invalid_upstream_response": ErrorSpec(
            "invalid_upstream_response",
            "上游响应格式不符合预期，代理无法安全解析。",
            502,
            "server_error",
            "invalid_response",
            False,
            False,
            "检查上游是否返回 OpenAI 兼容 JSON 或 SSE 格式。",
        ),
        "content_integrity_violation": ErrorSpec(
            "content_integrity_violation",
            "上游响应未通过内容完整性防护，已阻断返回。",
            422,
            "content_policy_error",
            "invalid_response",
            False,
            False,
            "切换可信提供商或联系管理员查看内容防护命中原因。",
            log_message="上游响应命中内容完整性高风险规则，已阻断并记录截断证据。",
            handling_strategy="检查 request_logs.content_guard_* 字段、provider 信任等级和内容完整性状态；恢复前不要重新放入普通路由。",
            alert_level="warning",
        ),
        "content_integrity_safe_error": ErrorSpec(
            "content_integrity_safe_error",
            "上游响应未通过内容完整性防护，系统已返回安全错误内容。",
            422,
            "content_policy_error",
            "invalid_response",
            False,
            False,
            "查看内容防护命中原因，必要时切换可信提供商或修正上游污染。",
            log_message="上游响应命中内容完整性高风险规则，safe_error 策略已返回安全错误内容。",
            handling_strategy="检查 request_logs.content_guard_* 字段、运行时 high_risk_strategy 和污染证据；确认恢复前不要重新放入普通路由。",
            alert_level="warning",
        ),
        "client_cancelled": ErrorSpec(
            "client_cancelled",
            "客户端已主动断开请求。",
            499,
            "client_error",
            "client_cancelled",
            False,
            False,
            "如仍需结果，请重新发起请求并保持连接。",
        ),
        "server_error": ErrorSpec(
            "server_error",
            "服务器暂时无法完成请求。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "携带 trace_id 联系管理员排查日志；若是临时故障，可稍后重试。",
        ),
        "internal_server_error": ErrorSpec(
            "internal_server_error",
            "服务器内部错误，请求未能完成。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "携带 trace_id 联系管理员排查日志；若是临时故障，可稍后重试。",
            public=False,
            public_code="server_error",
            public_message="服务器暂时无法完成请求，请稍后重试或携带 trace_id 联系管理员。",
            log_message="服务器内部错误，可能来自未分类异常、内部依赖失败或运行时状态异常。",
            handling_strategy="按 trace_id 查询异常堆栈、请求摘要、依赖可用、后台任务状态和最近配置变更；禁止把堆栈、Secret、数据库连接串或上游原文返回给用户。",
            alert_level="critical",
        ),
        "api_key_not_found": ErrorSpec(
            "api_key_not_found",
            "API Key 不存在或已被删除。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "刷新列表后确认对象是否仍存在。",
        ),
        "provider_not_found": ErrorSpec(
            "provider_not_found",
            "提供商不存在或已被删除。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "刷新列表后确认提供商是否仍存在。",
        ),
        "provider_model_not_found": ErrorSpec(
            "provider_model_not_found",
            "提供商模型绑定不存在或已被删除。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "刷新模型绑定列表后重新操作。",
        ),
        "user_not_found": ErrorSpec(
            "user_not_found",
            "用户不存在或已被删除。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "刷新用户列表后确认对象是否仍存在。",
        ),
        "conversation_not_found": ErrorSpec(
            "conversation_not_found",
            "会话记录不存在，或当前账号无权查看。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "刷新会话列表后重新选择可访问的会话。",
        ),
        "report_not_found": ErrorSpec(
            "report_not_found",
            "报告不存在、尚未生成或不在允许访问范围内。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "确认报告任务已完成，并从当前页面重新打开报告。",
        ),
        "benchmark_job_running": ErrorSpec(
            "benchmark_job_running",
            "已有压测任务正在运行，不能重复启动。",
            409,
            "conflict_error",
            "conflict",
            True,
            True,
            "等待当前压测任务结束，或先停止正在运行的任务。",
        ),
        "conflict": ErrorSpec(
            "conflict",
            "当前操作与系统中的现有状态发生冲突。",
            409,
            "conflict_error",
            "conflict",
            True,
            True,
            "根据当前状态调整输入后重试，或先处理冲突对象。",
        ),
        "unauthorized": ErrorSpec(
            "unauthorized",
            "当前未登录或登录态已失效。",
            401,
            "authentication_error",
            "authentication",
            False,
            False,
            "重新登录后再执行该操作。",
        ),
        "forbidden": ErrorSpec(
            "forbidden",
            "当前账号没有执行该操作的权限。",
            403,
            "authentication_error",
            "authorization",
            False,
            False,
            "切换到具备权限的账号，或联系管理员调整权限。",
        ),
    }

    STATUS_DEFAULT_CODES: dict[int, str] = {
        400: "invalid_request",
        401: "unauthorized",
        403: "forbidden",
        404: "resource_not_found",
        408: "request_timeout",
        409: "benchmark_job_running",
        413: "request_body_too_large",
        422: "request_validation_failed",
        429: "rate_limit_exceeded",
        499: "client_cancelled",
        500: "internal_server_error",
        502: "upstream_request_failed",
        503: "route_unavailable",
        504: "request_timeout",
    }

    MESSAGE_CODE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
        (("api key not found", "api key 不存在", "密钥不存在"), "api_key_not_found"),
        (("provider model not found", "提供商模型", "模型绑定不存在"), "provider_model_not_found"),
        (("provider not found", "提供商不存在"), "provider_not_found"),
        (("conversation not found", "会话不存在"), "conversation_not_found"),
        (("report", "报告"), "report_not_found"),
        (("用户不存在", "user not found", "owner_user_id does not exist"), "user_not_found"),
        (("model not found", "模型不存在", "unknown_model", "no such model"), "model_not_found"),
        (("unauthorized", "请先登录"), "unauthorized"),
        (("forbidden", "无权限", "permission"), "forbidden"),
        (("已有压测任务运行中", "already running"), "benchmark_job_running"),
        (("图片大小不能超过", "image too large"), "image_file_too_large"),
        (("上传文件不能为空", "empty image"), "empty_image_file"),
        (("仅支持 png", "仅支持 png/jpeg/webp/gif", "invalid image"), "invalid_image_file"),
        (("timeout", "超时"), "request_timeout"),
        (("rate limit", "too many requests", "限流"), "rate_limit_exceeded"),
        (("capacity", "并发", "容量"), "provider_capacity_exceeded"),
        (("redis",), "redis_unavailable"),
    )

    @classmethod
    def resolve(
        cls,
        *,
        status_code: int | None = None,
        detail: Any | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> ErrorSpec:
        extracted_code = cls.extract_code(detail) or code
        extracted_message = cls.extract_message(detail, fallback="") or (message or "")
        normalized_code = cls.normalize_code(extracted_code)
        if normalized_code in cls.ALIAS_CODES:
            normalized_code = cls.ALIAS_CODES[normalized_code]
        if normalized_code and normalized_code in cls.SPECS:
            return cls.SPECS[normalized_code]
        hinted_code = cls.infer_code_from_message(extracted_message, status_code=status_code)
        if hinted_code in cls.ALIAS_CODES:
            hinted_code = cls.ALIAS_CODES[hinted_code]
        if hinted_code in cls.SPECS:
            return cls.SPECS[hinted_code]
        default_code = cls.default_code_for_status(status_code)
        return cls.SPECS.get(default_code, cls._fallback_spec(status_code=status_code, code=normalized_code))

    @classmethod
    def build_error_object(
        cls,
        *,
        status_code: int | None,
        detail: Any | None = None,
        code: str | None = None,
        message: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        internal_spec = cls.resolve(status_code=status_code, detail=detail, code=code, message=message)
        spec = cls.public_spec_for(internal_spec)
        raw_message = cls.extract_message(detail, fallback="") or (message or "")
        raw_code = cls.extract_code(detail) or code
        if internal_spec.public:
            effective_message = cls.effective_message(spec, raw_message=raw_message)
        else:
            effective_message = internal_spec.public_message or spec.message
        error: dict[str, Any] = {
            "message": effective_message,
            "type": spec.error_type,
            "code": spec.code,
            "category": spec.category,
            "retryable": spec.retryable,
            "recoverable": spec.recoverable,
            "status_code": status_code or spec.status_code,
            "next_action": spec.next_action,
        }
        if trace_id:
            error["trace_id"] = trace_id
        if raw_code and raw_code != spec.code and internal_spec.public:
            error["raw_code"] = raw_code
        if raw_message and raw_message != effective_message and internal_spec.public:
            error["raw_message"] = raw_message
        if isinstance(detail, dict) and internal_spec.public:
            error["detail"] = detail
        elif isinstance(detail, dict):
            error["detail"] = {
                "message": "详细内部原因已写入服务端日志，请使用 trace_id 排查。",
            }
        return error

    @classmethod
    def normalize_detail(
        cls,
        *,
        status_code: int | None,
        detail: Any | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        internal_spec = cls.resolve(status_code=status_code, detail=detail, code=code, message=message)
        error = cls.build_error_object(status_code=status_code, detail=detail, code=code, message=message)
        normalized = {
            "message": error["message"],
            "code": error["code"],
            "category": error["category"],
            "retryable": error["retryable"],
            "recoverable": error["recoverable"],
            "next_action": error["next_action"],
        }
        if isinstance(detail, dict) and internal_spec.public:
            for key, value in detail.items():
                if key not in normalized:
                    normalized[key] = value
        elif isinstance(detail, dict):
            normalized["internal_detail_logged"] = True
        elif detail not in (None, ""):
            normalized["raw_detail"] = detail
        return normalized

    @classmethod
    def public_spec_for(cls, spec: ErrorSpec) -> ErrorSpec:
        if spec.public:
            return spec
        public_code = spec.public_code or "internal_server_error"
        if public_code in cls.ALIAS_CODES:
            public_code = cls.ALIAS_CODES[public_code]
        public_spec = cls.SPECS.get(public_code)
        if public_spec is not None and public_spec.public:
            return public_spec
        return cls.SPECS["internal_server_error"]

    @classmethod
    def build_log_context(
        cls,
        *,
        status_code: int | None,
        detail: Any | None = None,
        code: str | None = None,
        message: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        spec = cls.resolve(status_code=status_code, detail=detail, code=code, message=message)
        public_spec = cls.public_spec_for(spec)
        raw_message = cls.extract_message(detail, fallback="") or (message or "")
        raw_code = cls.extract_code(detail) or code
        log_context: dict[str, Any] = {
            "code": spec.code,
            "message": spec.log_message or spec.message,
            "public_code": public_spec.code,
            "public_message": spec.public_message or public_spec.message,
            "error_type": spec.error_type,
            "category": spec.category,
            "retryable": spec.retryable,
            "recoverable": spec.recoverable,
            "next_action": spec.next_action,
            "handling_strategy": spec.handling_strategy or spec.next_action,
            "alert_level": spec.alert_level,
            "public": spec.public,
            "status_code": status_code or spec.status_code,
        }
        if trace_id:
            log_context["trace_id"] = trace_id
        if raw_code and raw_code != spec.code:
            log_context["raw_code"] = raw_code
        if raw_message and raw_message != spec.message:
            log_context["raw_message"] = raw_message
        return log_context

    @classmethod
    def effective_message(cls, spec: ErrorSpec, *, raw_message: str | None) -> str:
        raw = (raw_message or "").strip()
        if not raw:
            return spec.message
        if spec.code.startswith("upstream_") and raw.lower() not in {"upstream request failed", "request failed"}:
            return raw
        if cls._contains_cjk(raw) and raw != spec.code:
            return raw
        return spec.message

    @staticmethod
    def extract_code(detail: Any) -> str | None:
        if isinstance(detail, dict):
            if isinstance(detail.get("code"), str) and detail["code"].strip():
                return detail["code"].strip()
            if isinstance(detail.get("error"), dict) and isinstance(detail["error"].get("code"), str):
                return detail["error"]["code"].strip()
        return None

    @staticmethod
    def extract_message(detail: Any, *, fallback: str) -> str:
        if isinstance(detail, dict):
            if isinstance(detail.get("error"), dict) and detail["error"].get("message"):
                return str(detail["error"]["message"])
            if isinstance(detail.get("message"), str) and detail["message"].strip():
                return detail["message"].strip()
            if isinstance(detail.get("detail"), str) and detail["detail"].strip():
                return detail["detail"].strip()
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        return fallback

    @classmethod
    def infer_code_from_message(cls, message: str | None, *, status_code: int | None = None) -> str:
        normalized = (message or "").strip().lower()
        if normalized:
            for tokens, code in cls.MESSAGE_CODE_HINTS:
                if any(token.lower() in normalized for token in tokens):
                    return code
        return cls.default_code_for_status(status_code)

    @classmethod
    def default_code_for_status(cls, status_code: int | None) -> str:
        if status_code in cls.STATUS_DEFAULT_CODES:
            return cls.STATUS_DEFAULT_CODES[int(status_code)]
        if status_code is not None and 500 <= int(status_code) < 600:
            return "internal_server_error"
        return "invalid_request"

    @staticmethod
    def normalize_code(code: str | None) -> str | None:
        if not code:
            return None
        return str(code).strip().lower().replace("-", "_").replace(" ", "_")

    @classmethod
    def _fallback_spec(cls, *, status_code: int | None, code: str | None) -> ErrorSpec:
        default_code = code or cls.default_code_for_status(status_code)
        if status_code is not None and int(status_code) in {408, 429, 500, 502, 503, 504}:
            return cls._complete_spec(ErrorSpec(
                default_code,
                "请求暂时无法完成，可能是上游、网络或服务端临时故障。",
                int(status_code),
                "server_error" if int(status_code) >= 500 else "rate_limit_error",
                "upstream_transient" if int(status_code) >= 500 else "rate_limit",
                True,
                True,
                "稍后重试；若持续失败，请携带 trace_id 联系管理员排查。",
            ))
        return cls._complete_spec(ErrorSpec(
            default_code,
            "请求失败，服务端已返回具体错误详情。",
            int(status_code or 400),
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "根据 detail 中的字段和值修正请求后重试。",
        ))

    @classmethod
    def _complete_spec(cls, spec: ErrorSpec) -> ErrorSpec:
        public_code = spec.public_code
        if public_code in cls.ALIAS_CODES:
            public_code = cls.ALIAS_CODES[public_code]
        if not spec.public and public_code == "internal_server_error":
            public_code = "server_error"
        if spec.public:
            public_message = spec.public_message or spec.message
        else:
            target_public_code = public_code or "internal_server_error"
            target_public_spec = cls.SPECS.get(target_public_code)
            public_message = (
                spec.public_message
                or (target_public_spec.message if target_public_spec is not None else "服务器内部错误，请求未能完成。")
            )
        log_message = spec.log_message or f"{spec.message} 错误码：{spec.code}。"
        handling_strategy = (
            spec.handling_strategy
            or cls.CATEGORY_HANDLING_STRATEGIES.get(spec.category)
            or spec.next_action
        )
        alert_level = spec.alert_level or "none"
        if alert_level == "none" and (not spec.public) and spec.status_code >= 500:
            alert_level = "warning"
        return ErrorSpec(
            code=spec.code,
            message=spec.message,
            status_code=spec.status_code,
            error_type=spec.error_type,
            category=spec.category,
            retryable=spec.retryable,
            recoverable=spec.recoverable,
            next_action=spec.next_action,
            public=spec.public,
            public_code=public_code,
            public_message=public_message,
            log_message=log_message,
            handling_strategy=handling_strategy,
            alert_level=alert_level,
        )

    @classmethod
    def finalize_specs(cls) -> None:
        cls.SPECS = {code: cls._complete_spec(spec) for code, spec in cls.SPECS.items()}

    @classmethod
    def validate_specs(cls) -> list[str]:
        errors: list[str] = []
        allowed_alert_levels = {"none", "info", "warning", "critical"}
        for code, spec in sorted(cls.SPECS.items()):
            required_values = {
                "code": spec.code,
                "message": spec.message,
                "error_type": spec.error_type,
                "category": spec.category,
                "next_action": spec.next_action,
                "public_message": spec.public_message,
                "log_message": spec.log_message,
                "handling_strategy": spec.handling_strategy,
                "alert_level": spec.alert_level,
            }
            for field_name, value in required_values.items():
                if not str(value or "").strip():
                    errors.append(f"{code}.{field_name} 缺失")
            if spec.code != code:
                errors.append(f"{code}.code 与目录键不一致：{spec.code}")
            if spec.alert_level not in allowed_alert_levels:
                errors.append(f"{code}.alert_level 不合法：{spec.alert_level}")
            if spec.public is False and not spec.public_code:
                errors.append(f"{code}.public_code 缺失")
            if spec.public_code and spec.public_code not in cls.SPECS:
                errors.append(f"{code}.public_code 指向不存在：{spec.public_code}")
            if spec.public is False and spec.public_code and not cls.SPECS[spec.public_code].public:
                errors.append(f"{code}.public_code 指向非公开错误：{spec.public_code}")
            if not cls._contains_cjk(spec.message):
                errors.append(f"{code}.message 必须包含中文说明")
            if spec.public_message and not cls._contains_cjk(spec.public_message):
                errors.append(f"{code}.public_message 必须包含中文说明")
            if spec.log_message and not cls._contains_cjk(spec.log_message):
                errors.append(f"{code}.log_message 必须包含中文说明")
            if spec.handling_strategy and not cls._contains_cjk(spec.handling_strategy):
                errors.append(f"{code}.handling_strategy 必须包含中文说明")
        return errors

    @staticmethod
    def _contains_cjk(value: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in value)


def _spec(
    code: str,
    message: str,
    status_code: int,
    error_type: str,
    category: str,
    retryable: bool,
    recoverable: bool,
    next_action: str,
    *,
    public: bool = True,
    public_code: str | None = None,
    public_message: str | None = None,
    log_message: str | None = None,
    handling_strategy: str | None = None,
    alert_level: str = "none",
) -> ErrorSpec:
    return ErrorSpec(
        code=code,
        message=message,
        status_code=status_code,
        error_type=error_type,
        category=category,
        retryable=retryable,
        recoverable=recoverable,
        next_action=next_action,
        public=public,
        public_code=public_code,
        public_message=public_message,
        log_message=log_message,
        handling_strategy=handling_strategy,
        alert_level=alert_level,
    )


ErrorCatalogService.SPECS.update(
    {
        "invalid_stream_mode": _spec(
            "invalid_stream_mode",
            "请求的 stream 参数与当前端点处理方式不匹配。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "按端点要求调整 stream 参数，或改用对应的流式/非流式入口。",
        ),
        "model_not_available": _spec(
            "model_not_available",
            "请求的模型当前不可用。",
            404,
            "invalid_request_error",
            "model_unavailable",
            False,
            False,
            "检查模型是否启用、是否有可用提供商挂载，以及当前 API Key 是否有权限。",
        ),
        "model_image_generation_not_available": _spec(
            "model_image_generation_not_available",
            "请求的模型当前不支持图片生成能力。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "改用支持图片生成的模型，或移除图片生成工具请求。",
        ),
        "stream_concurrency_exceeded": _spec(
            "stream_concurrency_exceeded",
            "当前流式并发请求数已达到限制。",
            429,
            "rate_limit_error",
            "capacity_limited",
            True,
            True,
            "等待已有流式请求结束后重试，或由管理员调整流式并发上限。",
        ),
        "source_ip_blocked": _spec(
            "source_ip_blocked",
            "当前来源 IP 已被 IP 管理规则拦截。",
            403,
            "authentication_error",
            "authorization",
            False,
            False,
            "联系管理员核对 IP 管理规则、可信代理配置和 API Key 来源限制。",
        ),
        "source_ip_rate_limited": _spec(
            "source_ip_rate_limited",
            "当前来源 IP 请求频率超过 IP 管理限制。",
            429,
            "rate_limit_error",
            "rate_limit",
            True,
            True,
            "降低请求频率，等待 IP 限流窗口恢复后重试。",
        ),
        "source_ip_resolution_failed": _spec(
            "source_ip_resolution_failed",
            "服务端无法安全解析当前请求来源 IP。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "核对反向代理转发头、可信代理 CIDR 和来源 IP 格式。",
        ),
        "stream_first_token_timeout": _spec(
            "stream_first_token_timeout",
            "流式响应首个 Token 等待超时。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "稍后重试，或检查上游首包延迟和模型负载。",
        ),
        "stream_idle_timeout": _spec(
            "stream_idle_timeout",
            "流式响应空闲时间超过系统上限。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "稍后重试，或检查上游是否长时间无数据输出。",
        ),
        "stream_max_duration_exceeded": _spec(
            "stream_max_duration_exceeded",
            "流式响应持续时间超过系统上限。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "缩短请求输出长度，或由管理员调整流式最大持续时间。",
        ),
        "stream_interrupted": _spec(
            "stream_interrupted",
            "流式响应在完成前中断。",
            502,
            "server_error",
            "upstream_transient",
            True,
            True,
            "查看 trace 中断位置，稍后重试或检查上游连接稳定性。",
        ),
        "upstream_stream_empty": _spec(
            "upstream_stream_empty",
            "上游流式响应没有返回有效数据。",
            502,
            "server_error",
            "invalid_response",
            True,
            True,
            "检查上游 SSE 输出格式、模型状态和代理缓冲设置。",
        ),
        "model_output_tokens_exceeded": _spec(
            "model_output_tokens_exceeded",
            "请求的最大输出 Token 超过目标模型输出上限。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "降低输出 Token 参数，或改用输出窗口更大的模型。",
        ),
        "long_output_requires_stream": _spec(
            "long_output_requires_stream",
            "请求的输出长度较大，必须使用流式响应。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "将 stream 设置为 true 后重新提交。",
        ),
        "invalid_image_count": _spec(
            "invalid_image_count",
            "图片数量参数不合法。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "按接口允许范围调整 n 或图片输入数量。",
        ),
        "invalid_image_prompt": _spec(
            "invalid_image_prompt",
            "图片提示词为空或不合法。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "填写有效的图片生成或编辑提示词。",
        ),
        "missing_image_input": _spec(
            "missing_image_input",
            "请求缺少图片输入。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "在请求中提供 image、image_url 或等价图片输入字段。",
        ),
        "missing_mask_input": _spec(
            "missing_mask_input",
            "请求缺少 mask 图片输入。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "在图片编辑请求中提供有效的 mask 输入，或移除需要 mask 的编辑模式。",
        ),
        "invalid_image_output_format": _spec(
            "invalid_image_output_format",
            "图片输出格式不受支持。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "将 output_format 调整为系统支持的格式。",
        ),
        "invalid_image_response_format": _spec(
            "invalid_image_response_format",
            "图片响应格式不受支持。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "将 response_format 调整为 url 或 b64_json 等受支持格式。",
        ),
        "invalid_image_output_compression": _spec(
            "invalid_image_output_compression",
            "图片压缩参数不合法。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "将 output_compression 调整为允许范围内的整数。",
        ),
        "responses_chat_previous_response_not_found": _spec(
            "responses_chat_previous_response_not_found",
            "未找到 previous_response_id 对应的兼容适配会话快照。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "确认 previous_response_id 是否来自同一平台且未过期，必要时重新开始会话。",
        ),
        "responses_chat_adapter_tool_round_limit_exceeded": _spec(
            "responses_chat_adapter_tool_round_limit_exceeded",
            "Responses→Chat 适配工具调用轮次超过系统上限。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "减少工具递归调用轮次，或由管理员调整适配层工具轮次上限。",
        ),
        "invalid_responses_input": _spec(
            "invalid_responses_input",
            "Responses input 字段结构不合法。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "按 Responses 协议提交字符串、对象或数组结构的 input。",
        ),
        "unsupported_endpoint_fallback": _spec(
            "unsupported_endpoint_fallback",
            "当前端点回退策略不支持本次请求。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "使用原生支持当前端点的提供商，或启用明确允许的兼容适配模式。",
        ),
        "endpoint_fallback_conversion_unsafe": _spec(
            "endpoint_fallback_conversion_unsafe",
            "当前端点回退转换不安全，已拒绝请求。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "移除无法转换的字段，或使用原生端点处理请求。",
        ),
        "provider_active_request_limit_exceeded": _spec(
            "provider_active_request_limit_exceeded",
            "提供商活跃请求数已达到上限。",
            429,
            "rate_limit_error",
            "capacity_limited",
            True,
            True,
            "等待提供商活跃请求下降后重试，或启用其它提供商分担流量。",
        ),
        "provider_active_stream_limit_exceeded": _spec(
            "provider_active_stream_limit_exceeded",
            "提供商流式活跃请求数已达到上限。",
            429,
            "rate_limit_error",
            "capacity_limited",
            True,
            True,
            "等待提供商流式请求释放后重试，或调整流式容量配置。",
        ),
        "provider_qps_limit_exceeded": _spec(
            "provider_qps_limit_exceeded",
            "提供商 QPS 已达到上限。",
            429,
            "rate_limit_error",
            "capacity_limited",
            True,
            True,
            "降低瞬时请求频率，等待短窗口恢复后重试。",
        ),
        "provider_rpm_limit_exceeded": _spec(
            "provider_rpm_limit_exceeded",
            "提供商 RPM 已达到上限。",
            429,
            "rate_limit_error",
            "capacity_limited",
            True,
            True,
            "等待分钟窗口恢复后重试，或调整提供商每分钟请求上限。",
        ),
        "chat_completion_not_found": _spec(
            "chat_completion_not_found",
            "Chat Completion 记录不存在或不可访问。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "确认 completion_id 是否正确，并检查当前 API Key 是否有访问权限。",
        ),
        "chat_completion_provider_not_available": _spec(
            "chat_completion_provider_not_available",
            "Chat Completion 管理请求没有可用提供商。",
            503,
            "server_error",
            "route_unavailable",
            True,
            True,
            "检查支持 Chat Completions 管理接口的提供商可用性与授权状态。",
        ),
        "moderation_not_found": _spec(
            "moderation_not_found",
            "Moderation 记录不存在或不可访问。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "确认 moderation_id 是否正确，并检查访问权限。",
        ),
        "moderation_provider_not_available": _spec(
            "moderation_provider_not_available",
            "Moderation 请求没有可用提供商。",
            503,
            "server_error",
            "route_unavailable",
            True,
            True,
            "检查支持 Moderation 的提供商可用性、模型挂载和授权状态。",
        ),
        "files_not_found": _spec(
            "files_not_found",
            "文件列表或文件资源不存在。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "刷新文件列表后重试，或确认文件是否已删除。",
        ),
        "file_not_found": _spec(
            "file_not_found",
            "文件不存在或当前账号无权访问。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "确认 file_id 是否正确，并检查当前 API Key 的访问范围。",
        ),
        "file_content_not_found": _spec(
            "file_content_not_found",
            "文件内容不存在或无法读取。",
            404,
            "invalid_request_error",
            "resource_not_found",
            False,
            False,
            "确认文件已上传完成且未被清理，再重新读取内容。",
        ),
        "files_provider_not_available": _spec(
            "files_provider_not_available",
            "Files 请求没有可用提供商。",
            503,
            "server_error",
            "route_unavailable",
            True,
            True,
            "检查支持 Files API 的提供商可用性、授权和上游配置。",
        ),
        "image_url_fetch_failed": _spec(
            "image_url_fetch_failed",
            "无法拉取图片 URL 内容。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "确认图片 URL 可公开访问、未过期，且返回的是受支持图片格式。",
        ),
        "image_url_too_large_for_inline_conversion": _spec(
            "image_url_too_large_for_inline_conversion",
            "图片 URL 内容过大，无法安全内联转换。",
            413,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "压缩图片或改用可由上游直接访问的图片 URL。",
        ),
        "image_result_missing": _spec(
            "image_result_missing",
            "图片生成响应缺少可用图片结果。",
            502,
            "server_error",
            "invalid_response",
            False,
            False,
            "检查上游图片生成响应结构和 trace 中的原始摘要。",
            log_message="上游图片生成响应缺少可提取的 url 或 b64_json 图片结果。",
            handling_strategy="记录上游响应结构摘要，核对图片工具调用结果字段，避免把完整 base64 写入日志。",
        ),
        "image_result_b64_unavailable": _spec(
            "image_result_b64_unavailable",
            "图片结果缺少可返回的 base64 内容。",
            502,
            "server_error",
            "invalid_response",
            False,
            False,
            "改用 URL 结果格式，或检查上游是否支持返回 b64_json。",
            log_message="上游图片结果无法提供 b64_json，可能只返回 URL 或字段缺失。",
            handling_strategy="记录结果字段摘要和 response_format，避免记录完整图片二进制。",
        ),
        "legacy_image_result_missing": _spec(
            "legacy_image_result_missing",
            "Legacy Images 兼容层没有返回可解析图片结果。",
            502,
            "server_error",
            "invalid_response",
            False,
            False,
            "优先迁移到 /v1/responses 图片工具，或检查兼容层结果映射。",
        ),
        "legacy_images_stream_not_supported": _spec(
            "legacy_images_stream_not_supported",
            "Legacy Images API 不支持流式响应。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "移除 stream 参数，或改用 /v1/responses 图片工具流式能力。",
        ),
        "missing_function_call_output_call_id": _spec(
            "missing_function_call_output_call_id",
            "function_call_output 缺少 call_id。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "为每个 function_call_output 提供对应工具调用的 call_id。",
        ),
        "responses_chat_adapter_unsupported_input_type": _spec(
            "responses_chat_adapter_unsupported_input_type",
            "Responses→Chat 适配层不支持当前 input 类型。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "将 input 改为文本、图片输入或可适配的消息对象。",
        ),
        "responses_chat_adapter_web_search_disabled": _spec(
            "responses_chat_adapter_web_search_disabled",
            "Responses→Chat 适配层未启用 web_search。",
            400,
            "invalid_request_error",
            "capability_not_supported",
            False,
            False,
            "移除 web_search 工具，或由管理员显式启用搜索代理。",
        ),
        "responses_chat_adapter_web_search_query_missing": _spec(
            "responses_chat_adapter_web_search_query_missing",
            "web_search 工具调用缺少搜索查询内容。",
            400,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "提供明确搜索关键词后重试。",
        ),
        "responses_chat_adapter_stream_failed": _spec(
            "responses_chat_adapter_stream_failed",
            "Responses→Chat 适配流式响应失败。",
            502,
            "server_error",
            "upstream_transient",
            True,
            True,
            "查看 trace 中的适配阶段和上游错误，必要时稍后重试。",
        ),
        "responses_chat_adapter_web_search_proxy_not_configured": _spec(
            "responses_chat_adapter_web_search_proxy_not_configured",
            "搜索代理未配置。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "配置搜索代理地址后重试。",
            public=False,
            public_code="capability_not_supported",
            public_message="当前 web_search 能力暂不可用。",
            log_message="Responses→Chat 适配层启用了 web_search，但未配置搜索代理地址。",
            handling_strategy="检查 responses_chat_adapter_search_proxy_url 和环境变量配置。",
            alert_level="warning",
        ),
        "responses_chat_adapter_web_search_proxy_failed": _spec(
            "responses_chat_adapter_web_search_proxy_failed",
            "搜索代理调用失败。",
            502,
            "server_error",
            "upstream_transient",
            True,
            True,
            "恢复搜索代理服务后重试。",
            public=False,
            public_code="upstream_request_failed",
            public_message="当前 web_search 能力暂时请求失败。",
            log_message="Responses→Chat 适配层搜索代理调用失败。",
            handling_strategy="检查搜索代理状态、网络、鉴权和返回格式。",
            alert_level="warning",
        ),
        "responses_chat_adapter_snapshot_too_large": _spec(
            "responses_chat_adapter_snapshot_too_large",
            "Responses→Chat 适配会话快照超过存储上限。",
            413,
            "invalid_request_error",
            "invalid_request",
            False,
            False,
            "缩短会话上下文或启用截断策略后重试。",
            public=False,
            public_code="invalid_request",
            public_message="会话上下文过长，当前请求无法继续。",
            log_message="适配会话快照超过 responses_chat_adapter_snapshot_max_bytes。",
            handling_strategy="调整快照上限、清理旧会话或提示用户缩短上下文。",
            alert_level="warning",
        ),
        "provider_capacity_unavailable": _spec(
            "provider_capacity_unavailable",
            "提供商容量状态不可用。",
            503,
            "server_error",
            "capacity_limited",
            True,
            True,
            "恢复容量状态服务后重试。",
            public=False,
            public_code="route_unavailable",
            public_message="系统繁忙或提供商容量暂时不可用。",
            log_message="提供商容量状态读取失败，通常与 Redis、容量快照或租约状态有关。",
            handling_strategy="检查 Redis 连接、容量脚本执行结果、租约释放和容量快照刷新任务。",
            alert_level="critical",
        ),
        "upstream_connect_timeout": _spec(
            "upstream_connect_timeout",
            "连接上游服务超时。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "检查上游网络、DNS、Base URL 和连接超时配置。",
        ),
        "upstream_write_timeout": _spec(
            "upstream_write_timeout",
            "向上游发送请求体超时。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "检查上游接收速度、请求体大小和写超时配置。",
        ),
        "upstream_pool_timeout": _spec(
            "upstream_pool_timeout",
            "等待上游连接池可用连接超时。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "增大连接池或降低并发，检查连接是否及时释放。",
        ),
        "upstream_timeout": _spec(
            "upstream_timeout",
            "上游请求超时。",
            504,
            "timeout_error",
            "timeout",
            True,
            True,
            "根据 trace 判断连接、写入、读取或连接池阶段后处理。",
        ),
        "upstream_network_error": _spec(
            "upstream_network_error",
            "上游网络请求失败。",
            502,
            "server_error",
            "network",
            True,
            True,
            "检查网络连通性、代理配置、TLS 和上游服务状态。",
        ),
        "request_log_queue_initialization_failed": _spec(
            "request_log_queue_initialization_failed",
            "请求日志队列初始化失败。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "修复日志队列依赖后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="请求日志可靠队列初始化失败。",
            handling_strategy="检查 Redis Stream 配置、连接权限和后台 worker 启动状态。",
            alert_level="critical",
        ),
        "request_log_persist_failed": _spec(
            "request_log_persist_failed",
            "请求日志落库失败。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "恢复数据库写入后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="请求日志写入 request_logs 失败。",
            handling_strategy="检查数据库连接、字段迁移、队列积压和同步落库回退。",
            alert_level="critical",
        ),
        "token_finalize_failed": _spec(
            "token_finalize_failed",
            "Token 统计补全失败。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "修复 Token 补全任务后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="Token finalize 后台任务失败。",
            handling_strategy="检查 request_logs usage 字段、tokenizer 可用性、重试次数和积压指标。",
            alert_level="warning",
        ),
        "billing_finalize_failed": _spec(
            "billing_finalize_failed",
            "计费补全失败。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "修复计费后台任务后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="计费补全或余额扣减后台任务失败。",
            handling_strategy="检查幂等事件、账户余额、价格字段和 billing_attempt_count。",
            alert_level="critical",
        ),
        "worker_exception": _spec(
            "worker_exception",
            "后台 worker 发生异常。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "检查后台 worker 日志并恢复任务消费。",
            public=False,
            public_code="internal_server_error",
            log_message="后台 worker 捕获未预期异常。",
            handling_strategy="定位任务类型、重试次数、队列积压和异常堆栈。",
            alert_level="warning",
        ),
        "db_connection_failed": _spec(
            "db_connection_failed",
            "数据库连接失败。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "恢复数据库连接后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="数据库连接或连接池不可用。",
            handling_strategy="检查 DATABASE_URL、连接池、数据库实例状态和网络。",
            alert_level="critical",
        ),
        "db_transaction_rollback": _spec(
            "db_transaction_rollback",
            "数据库事务已回滚。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "排查事务失败原因后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="数据库事务回滚。",
            handling_strategy="检查约束冲突、字段类型、并发写入和异常链。",
            alert_level="warning",
        ),
        "db_unique_constraint_conflict": _spec(
            "db_unique_constraint_conflict",
            "数据库唯一键冲突。",
            409,
            "conflict_error",
            "conflict",
            False,
            False,
            "使用不重复的唯一字段后重试。",
            public=False,
            public_code="conflict",
            public_message="当前操作与已有数据冲突。",
            log_message="数据库唯一键约束冲突。",
            handling_strategy="确认唯一字段、并发创建路径和前置重复校验。",
            alert_level="none",
        ),
        "cache_invalidated": _spec(
            "cache_invalidated",
            "缓存已失效。",
            200,
            "server_event",
            "cache",
            False,
            True,
            "重新读取最新数据即可。",
            public=False,
            public_code="internal_server_error",
            log_message="缓存失效事件。",
            handling_strategy="确认相关写操作是否主动失效缓存，观察命中率是否恢复。",
        ),
        "queue_backlog": _spec(
            "queue_backlog",
            "后台队列积压。",
            503,
            "server_error",
            "server_error",
            True,
            True,
            "等待队列消费恢复或扩容 worker。",
            public=False,
            public_code="internal_server_error",
            log_message="后台队列积压超过阈值。",
            handling_strategy="检查 worker 数量、单任务耗时、Redis Stream 长度和失败重试。",
            alert_level="warning",
        ),
        "provider_health_probe_failed": _spec(
            "provider_health_probe_failed",
            "提供商可用性探针失败。",
            503,
            "server_error",
            "upstream_transient",
            True,
            True,
            "修复提供商可用性探针失败原因后重试。",
            public=False,
            public_code="route_unavailable",
            log_message="提供商可用性检测或端点探针失败。",
            handling_strategy="检查探针端点、模型名、API Key、协议类型和上游响应摘要。",
            alert_level="warning",
        ),
        "provider_capacity_snapshot_failed": _spec(
            "provider_capacity_snapshot_failed",
            "提供商容量快照生成失败。",
            503,
            "server_error",
            "capacity_limited",
            True,
            True,
            "恢复容量快照任务后重试。",
            public=False,
            public_code="route_unavailable",
            log_message="提供商容量快照刷新失败。",
            handling_strategy="检查 Redis 计数、租约集合、快照生成任务和 provider 容量配置。",
            alert_level="warning",
        ),
        "upstream_raw_error": _spec(
            "upstream_raw_error",
            "上游返回原始错误。",
            502,
            "server_error",
            "upstream_transient",
            True,
            True,
            "查看日志中的上游响应摘要后处理。",
            public=False,
            public_code="upstream_request_failed",
            public_message="上游服务返回失败。",
            log_message="上游返回堆栈、HTML、非标准 JSON 或底层异常细节。",
            handling_strategy="仅记录截断摘要，禁止把原始敏感内容直接返回给调用方。",
            alert_level="warning",
        ),
        "deployment_config_invalid": _spec(
            "deployment_config_invalid",
            "部署配置不合规。",
            500,
            "server_error",
            "server_error",
            False,
            False,
            "修复部署配置后重新启动服务。",
            public=False,
            public_code="internal_server_error",
            log_message="部署或环境变量配置不符合生产要求。",
            handling_strategy="检查 REDIS_URL、Secret、数据库类型、反向代理和环境变量。",
            alert_level="critical",
        ),
        "secret_invalid": _spec(
            "secret_invalid",
            "生产环境 Secret 不合规。",
            500,
            "server_error",
            "server_error",
            False,
            False,
            "使用高强度随机 Secret 后重新启动服务。",
            public=False,
            public_code="internal_server_error",
            log_message="生产环境 Secret 缺失、默认值或强度不足。",
            handling_strategy="轮换 SESSION_SECRET_KEY、API_KEY_ENCRYPTION_SECRET 等关键密钥。",
            alert_level="critical",
        ),
        "postgresql_required": _spec(
            "postgresql_required",
            "应用必须使用 PostgreSQL 数据库启动。",
            500,
            "server_error",
            "server_error",
            False,
            False,
            "配置 PostgreSQL DATABASE_URL 后重新启动服务。",
            public=False,
            public_code="internal_server_error",
            log_message="检测到不受支持的数据库配置。",
            handling_strategy="将 DATABASE_URL 配置为 postgresql+psycopg:// 等 PostgreSQL 连接地址。",
            alert_level="critical",
        ),
        "nginx_sse_buffering_enabled": _spec(
            "nginx_sse_buffering_enabled",
            "Nginx SSE 缓冲配置不正确。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "关闭 SSE 相关缓冲后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="反向代理可能启用了 SSE 缓冲，影响流式响应。",
            handling_strategy="检查 proxy_buffering、proxy_request_buffering 和超时配置。",
            alert_level="warning",
        ),
        "serialization_failed": _spec(
            "serialization_failed",
            "序列化失败。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "修复不可序列化字段后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="对象序列化为 JSON、日志或缓存值时失败。",
            handling_strategy="检查 Decimal、datetime、bytes、异常对象和循环引用。",
            alert_level="warning",
        ),
        "assertion_failed": _spec(
            "assertion_failed",
            "内部断言失败。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "根据断言位置修复内部状态不一致。",
            public=False,
            public_code="internal_server_error",
            log_message="内部断言失败。",
            handling_strategy="定位断言条件、输入状态和调用链，补充防御性校验。",
            alert_level="critical",
        ),
        "third_party_library_error": _spec(
            "third_party_library_error",
            "第三方库异常。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "检查第三方库异常原因后重试。",
            public=False,
            public_code="internal_server_error",
            log_message="第三方库调用发生异常。",
            handling_strategy="记录库名、版本、输入摘要和异常类型，避免暴露敏感参数。",
            alert_level="warning",
        ),
        "unhandled_exception": _spec(
            "unhandled_exception",
            "未捕获异常。",
            500,
            "server_error",
            "server_error",
            True,
            True,
            "携带 trace_id 排查服务端日志。",
            public=False,
            public_code="internal_server_error",
            public_message="服务器内部错误，请求未能完成。",
            log_message="请求链路出现未捕获异常。",
            handling_strategy="按 trace_id 查找异常堆栈、请求摘要、当前 provider 和后台任务状态。",
            alert_level="critical",
        ),
        "daily_request_quota_exhausted": _spec(
            "daily_request_quota_exhausted",
            "API Key 今日请求次数已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待日窗口恢复，或联系管理员确认是否仍启用历史请求次数配额。",
            log_message="API Key 日请求次数历史配额拦截。",
            handling_strategy="核对 RateLimitService 是否仍在当前链路启用历史请求次数配额；若项目已切换为余额唯一拦截，应评估清理该旧配置。",
        ),
        "api_key_token_quota_exhausted": _spec(
            "api_key_token_quota_exhausted",
            "API Key Token 总量已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "联系管理员确认 Token 配额配置，或改用余额充足且未受历史配额限制的密钥。",
            log_message="API Key Token 总量历史配额拦截。",
            handling_strategy="核对 API Key Token 总量配额是否仍应生效；按现行余额优先规范确认是否需要移除旧配额拦截。",
        ),
        "daily_token_quota_exhausted": _spec(
            "daily_token_quota_exhausted",
            "API Key 今日 Token 用量已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待日窗口恢复，或联系管理员确认历史 Token 日配额配置。",
            log_message="API Key 日 Token 历史配额拦截。",
            handling_strategy="核对 RateLimitService 日 Token 统计、窗口边界和旧配额配置；确认是否与余额唯一拦截规范冲突。",
        ),
        "api_key_cost_quota_exhausted": _spec(
            "api_key_cost_quota_exhausted",
            "API Key 消费金额已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "联系管理员确认金额配额配置，或使用账户余额治理方式处理。",
            log_message="API Key 金额历史配额拦截。",
            handling_strategy="核对 API Key 金额配额配置与账户余额口径；按现行规范优先使用账户可用余额作为消费拦截依据。",
        ),
        "daily_cost_quota_exhausted": _spec(
            "daily_cost_quota_exhausted",
            "API Key 今日消费金额已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待日窗口恢复，或联系管理员确认历史金额日配额配置。",
            log_message="API Key 日金额历史配额拦截。",
            handling_strategy="核对日消费统计、费用补全任务和旧金额日配额配置；确认是否需要按余额唯一拦截规范清理。",
        ),
        "tpm_limit_exceeded": _spec(
            "tpm_limit_exceeded",
            "Token 每分钟速率已达到限制。",
            429,
            "rate_limit_error",
            "rate_limit",
            True,
            True,
            "降低单位时间内的输入规模或请求频率，等待分钟窗口恢复后重试。",
            log_message="TPM 短窗口限流触发。",
            handling_strategy="核对每分钟 Token 统计窗口、估算 Token、Redis 计数和限流阈值，确认是否存在大上下文突增。",
        ),
        "account_request_quota_exhausted": _spec(
            "account_request_quota_exhausted",
            "账户请求次数已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "联系管理员确认账户请求次数配额，或按账户余额治理方式调整限制。",
            log_message="账户请求次数历史配额拦截。",
            handling_strategy="核对账户级请求次数总配额是否仍应生效；按现行余额唯一拦截规范评估旧配额清理。",
        ),
        "account_daily_request_quota_exhausted": _spec(
            "account_daily_request_quota_exhausted",
            "账户今日请求次数已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待日窗口恢复，或联系管理员确认账户日请求次数配额。",
            log_message="账户日请求次数历史配额拦截。",
            handling_strategy="核对账户日请求统计、窗口边界和旧配额配置；确认是否与余额唯一拦截规范冲突。",
        ),
        "account_monthly_request_quota_exhausted": _spec(
            "account_monthly_request_quota_exhausted",
            "账户本月请求次数已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待月窗口恢复，或联系管理员确认账户月请求次数配额。",
            log_message="账户月请求次数历史配额拦截。",
            handling_strategy="核对账户月请求统计、自然月窗口和旧配额配置；确认是否需要迁移为余额治理。",
        ),
        "account_token_quota_exhausted": _spec(
            "account_token_quota_exhausted",
            "账户 Token 总量已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "联系管理员确认账户 Token 总量配额，或按账户余额治理方式调整限制。",
            log_message="账户 Token 总量历史配额拦截。",
            handling_strategy="核对账户 Token 总量统计和旧配额配置；按现行余额唯一拦截规范评估是否移除。",
        ),
        "account_daily_token_quota_exhausted": _spec(
            "account_daily_token_quota_exhausted",
            "账户今日 Token 用量已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待日窗口恢复，或联系管理员确认账户日 Token 配额。",
            log_message="账户日 Token 历史配额拦截。",
            handling_strategy="核对账户日 Token 统计、请求 Token 估算和旧配额配置；确认是否需要按余额口径收口。",
        ),
        "account_monthly_token_quota_exhausted": _spec(
            "account_monthly_token_quota_exhausted",
            "账户本月 Token 用量已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待月窗口恢复，或联系管理员确认账户月 Token 配额。",
            log_message="账户月 Token 历史配额拦截。",
            handling_strategy="核对账户月 Token 统计、自然月窗口和旧配额配置；确认是否需要迁移为余额治理。",
        ),
        "account_cost_quota_exhausted": _spec(
            "account_cost_quota_exhausted",
            "账户消费金额已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "联系管理员确认账户金额配额，或改用账户可用余额治理。",
            log_message="账户金额历史配额拦截。",
            handling_strategy="核对账户金额配额、冻结金额和余额口径；按现行规范优先以账户可用余额作为消费拦截依据。",
        ),
        "account_daily_cost_quota_exhausted": _spec(
            "account_daily_cost_quota_exhausted",
            "账户今日消费金额已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待日窗口恢复，或联系管理员确认账户日金额配额。",
            log_message="账户日金额历史配额拦截。",
            handling_strategy="核对账户日消费统计、费用补全任务和旧配额配置；确认是否需要按余额唯一拦截规范清理。",
        ),
        "account_monthly_cost_quota_exhausted": _spec(
            "account_monthly_cost_quota_exhausted",
            "账户本月消费金额已达到历史配额上限。",
            429,
            "rate_limit_error",
            "quota",
            True,
            True,
            "等待月窗口恢复，或联系管理员确认账户月金额配额。",
            log_message="账户月金额历史配额拦截。",
            handling_strategy="核对账户月消费统计、自然月窗口和旧配额配置；确认是否需要迁移为账户余额治理。",
        ),
    }
)

ErrorCatalogService.STATUS_DEFAULT_CODES.update(
    {
        425: "upstream_request_failed",
    }
)

ErrorCatalogService.finalize_specs()
