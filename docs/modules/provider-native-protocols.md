# 提供商原生协议与模型分组

摘要：本文记录提供商模型挂载的端点协议、模型分组、Gemini/Claude 原生协议适配、探针边界和日志口径。关键词：`provider`、`ProviderModel`、`protocol_type`、`model_group`、`gemini`、`claude_messages`、`health_probe`、`content_guard_probe`。

最后维护时间：2026-06-13

## 官方依据

- Gemini API 官方文档以 `models/{model}:generateContent` 作为非流式文本/多模态生成入口，流式入口为 `streamGenerateContent`，请求体使用 `contents`、`parts`、`systemInstruction`、`generationConfig` 等结构，API Key 可通过 `x-goog-api-key` 传递。参考：<https://ai.google.dev/api/generate-content>、<https://ai.google.dev/gemini-api/docs/text-generation>。
- Anthropic Claude Messages API 官方文档以 `POST /v1/messages` 作为消息生成入口，请求头包含 `x-api-key` 与 `anthropic-version`，请求体包含 `model`、`max_tokens`、`messages`，系统提示使用顶层 `system` 字段，内容使用 text/image 等 content block。参考：<https://docs.anthropic.com/en/api/messages>、<https://docs.anthropic.com/en/docs/build-with-claude/streaming>。
- Prompt caching 口径参考官方文档：OpenAI 自动前缀缓存 <https://platform.openai.com/docs/guides/prompt-caching>；Claude Prompt caching <https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching>；Gemini Context caching <https://ai.google.dev/gemini-api/docs/caching>；DeepSeek KV Cache <https://api-docs.deepseek.com/guides/kv_cache>；智谱 GLM Cache <https://docs.bigmodel.cn/cn/guide/capabilities/cache>。

## 默认端点协议矩阵

| 模型系列 | `model_group` | 默认 `protocol_type` | OpenAI Chat | OpenAI Responses | 原生协议 |
| --- | --- | --- | --- | --- | --- |
| GPT / OpenAI `gpt-*`、`o1`、`o3`、`o4` | `openai` | `both` | 支持 | 支持 | 不适用 |
| 国产主流模型 DeepSeek、Qwen、GLM、豆包、Kimi、百川、文心、混元、MiniMax、阶跃、书生浦语、讯飞星火、Yi 等 | 对应品牌，如 `deepseek`、`qwen` | `chat_completions` | 支持 | 不默认支持 | 不适用 |
| Gemini | `gemini` | `gemini` | 不直接探测 | 不直接探测 | Gemini `generateContent` / `streamGenerateContent` |
| Claude | `claude` | `claude_messages` | 不直接探测 | 不直接探测 | Claude Messages API |

新增模型挂载时必须自动推断 `model_group`，同时允许管理员在前端模型挂载编辑中人工调整。若名称无法识别，使用 `unknown`，但新增长期可用模型时应优先补充分组规则。

## 请求适配规则

- 对外入口仍保持 `/v1/chat/completions`、`/v1/responses`、`/v1/completions` 等 OpenAI 兼容路径，外部调用方不直接感知上游原生协议。
- Gemini/Claude 模型挂载命中后，由 `NativeProtocolAdapter` 把 OpenAI 兼容请求转换为对应官方原生请求，再把上游响应转换回 OpenAI 兼容响应。
- Gemini 鉴权头使用 `x-goog-api-key`，Claude 鉴权头使用 `x-api-key` 与 `anthropic-version: 2023-06-01`；原生协议请求不得叠加 OpenAI `Authorization: Bearer ...` 到上游。
- 模型挂载可配置 `native_endpoint_path` 作为 Gemini/Claude 原生协议的自定义接口路径模板；为空时使用官方默认路径。模板支持 `{model}`、`{raw_model}`、`{action}`，其中 Gemini 默认非流式 action 为 `generateContent`，流式 action 为 `streamGenerateContent?alt=sse`。历史提供商级 `native_endpoint_path` 只作为兼容兜底读取，不再作为新增/编辑表单的主配置口径。
- `native_endpoint_path` 只填写路径部分，域名、协议和网关前缀仍由 `base_url` 承载；例如 Gemini 官方兼容路径可为空，自定义网关可填 `/proxy/google/{model}:{action}`，Claude 网关可填 `/anthropic/messages`。
- “官方原生端点协议”只约束请求结构、鉴权头、路径模板语义和响应适配，不强制使用官方固定域名；实际上游地址必须继续由提供商 `base_url` 决定，并允许通过网关、代理或聚合商地址承载。
- 原生协议流式响应必须转换为 OpenAI Chat SSE chunk，再按原请求入口需要转换为 Responses 或 Completions 流式结构。
- 若模型挂载存在上游真实模型名配置，健康检测、内容防护探针和代理转发必须统一使用该上游真实模型名；展示、权限和日志中的本项目模型别名仍保留为 `model_name`。

## Usage、成本与速率映射

- Gemini/Claude 官方 usage 不是 GPT usage 的同一结构。适配层只能把语义等价字段转换为现有日志和计费字段，同时必须在 `usage.native_usage` 保留官方原始字段，并通过 `usage.usage_schema` 标记来源。
- Gemini 原生响应的 `usageMetadata.promptTokenCount` 语义等价于输入 token，映射为 `usage.prompt_tokens` / `input_tokens`；`candidatesTokenCount` 语义等价于输出 token，映射为 `completion_tokens` / `output_tokens`；`totalTokenCount` 映射为 `total_tokens`。
- Gemini 的 `cachedContentTokenCount` 语义等价于缓存读取 token，映射为 `cache_read_tokens` 与 `prompt_tokens_details.cached_tokens`；`thoughtsTokenCount` 映射为 `reasoning_tokens` 与 `completion_tokens_details.reasoning_tokens`，并保留原字段名。
- Claude Messages 的 `usage.input_tokens` 表示非缓存输入 token，`cache_read_input_tokens` 表示缓存读取 token，`cache_creation_input_tokens` 表示缓存写入 token；日志里的 `prompt_tokens` 必须等于三者之和，确保成本按普通输入、缓存读取、缓存写入拆分计费。
- Claude 的 `usage.output_tokens` 映射为 `completion_tokens` / `output_tokens`；`total_tokens` 为输入 token、缓存读写 token 与输出 token 的合计。
- 不得把含义不同或官方未提供的字段硬转成 GPT 字段；例如 Gemini/Claude 的官方 usage 原文必须保留，未知新增字段只进入 `native_usage`，待确认语义后再增加等价映射。
- 请求日志、计费与速率展示继续复用现有 `LogService`、`BillingService` 和前端日志展示：只要适配层输出标准 `usage`，即可自动计算总成本、Token 消耗、缓存 Token、耗时、首 Token 与 TPS。

## Prompt caching 口径

- OpenAI GPT、DeepSeek、智谱 GLM 及多数 OpenAI 兼容国内渠道主要依赖稳定 prompt 前缀自动命中缓存。代理层必须保持客户端提交的 `messages`、`input`、`tools`、`instructions` 等高复用前缀的顺序和内容稳定；除协议兼容必需转换外，禁止重排、拆分、随机注入或改写静态前缀。
- Claude 原生协议支持显式 content block `cache_control` 与顶层自动缓存控制。适配层必须保留客户端显式 `cache_control`；当请求没有显式断点时，允许为 Claude 原生请求补充 `cache_control: {"type": "ephemeral"}`，提升长上下文、多轮会话和重复系统提示的缓存命中率。
- Gemini 原生协议支持显式 CachedContent 引用。适配层必须透传客户端提交的 `cachedContent` 或 `cached_content` 到 Gemini 原生请求体，禁止在 OpenAI 兼容入参转原生请求时丢弃已创建的缓存引用。
- DashScope/Qwen 等 OpenAI 兼容渠道如支持 `cache_control`，通用 OpenAI 兼容转发层只负责透传客户端原始字段；禁止在所有 OpenAI 兼容提供商上默认强塞 `cache_control`，避免不支持该扩展字段的聚合渠道或官方兼容端点返回参数错误。
- 日志与计费必须兼容多厂商缓存字段：OpenAI `prompt_tokens_details.cached_tokens`、Claude `cache_read_input_tokens` / `cache_creation_input_tokens`、Gemini `cachedContentTokenCount`、DeepSeek/GLM 常见 `prompt_cache_hit_tokens` / `cached_tokens`、DashScope/Qwen 常见 `input_tokens_details.cached_tokens` / `cache_creation_tokens`。

## 探针边界

- 模型端点协议检测只允许检测 OpenAI 兼容挂载的 `/chat/completions` 与 `/responses`，不得对 Gemini/Claude 发起 Chat/Responses 探测，也不得对原生端点写入端点协议沉淀结论。
- Gemini/Claude 无需端点协议检测。其可用性由原生健康检测维护，内容可信度由原生内容防护/可信探针维护。
- 原生健康检测使用 `NativeProtocolAdapter.native_text_payload` 与官方原生端点，必须请求 `Accept-Encoding: identity`。
- 内容防护可信探针对 OpenAI 兼容模型按配置优先使用 Chat 或 Responses；对 Gemini/Claude 必须走 `/native/gemini` 或 `/native/claude_messages` 适配入口，再由适配层生成官方原生请求。
- 探针结果应记录 `protocol_type`、`endpoint_path`、原始上游响应摘要和内容防护判定摘要，便于日志中心追溯。

## 前端配置口径

- 提供商新增/编辑表单只展示提供商基础信息、`provider_type`、`base_url`、`api_key`、渠道分组和模型挂载列表；`provider_type` 只是提供商分类，下拉应包含 `OpenAI`、`OpenAI-Response`、`Gemini`、`Anthropic`、`Azure OpenAI`、`New API`、`CherryIN`、`Ollama` 等常见类型，不代表具体模型端点能力。
- 模型挂载行、模型挂载矩阵和模型管理页面必须展示并保存 `protocol_type`、`model_group` 与 `native_endpoint_path`；模型挂载矩阵筛选区必须支持按 `model_group` 过滤挂载记录。
- 模型挂载分组为 Gemini 或 Claude 时，`protocol_type` 必须分别锁定为 `gemini` 或 `claude_messages`，前端不得允许人工改成 Chat/Responses/双协议，后端保存接口也必须按模型分组强制归一化；管理员只能通过修改模型分组来解除该协议锁定。
- 模型挂载协议下拉应包含 `双协议`、`Chat`、`Responses`、`Gemini`、`Claude`。
- 模型分组下拉应包含国内外主流品牌分组，如 `openai`、`deepseek`、`qwen`、`glm`、`doubao`、`kimi`、`gemini`、`claude` 等。
- 对 Gemini/Claude 的协议检测按钮应跳过端点协议检测，并提示使用原生健康检测和可信检测。

## 缓存与一致性

- 新增、编辑提供商或模型挂载后，保存接口必须先快速持久化，再异步触发健康、可信或协议检测。
- 修改模型协议、分组或能力后，必须调用提供商运行时缓存失效逻辑，避免路由继续读取旧能力。
- 高频模型目录和提供商轻量列表继续使用现有缓存；不得为 Gemini/Claude 另建重复缓存体系。
