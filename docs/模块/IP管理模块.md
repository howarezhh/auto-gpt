# IP 管理模块

摘要：IP 管理模块负责可信代理解析、IP 规则匹配、IP 限流、处置事件记录、请求日志关联和后台测试接口。关键词：`IpManagementMiddleware`、`IpManagementService`、`ClientIpResolver`、`IpManagementEventService`、`ip_access_rules`、`ip_management_events`、`external_v1`、`internal_api`、`user_pages`。

最后维护时间：2026-06-15

## 模块边界

- 中间件入口：`app/middleware/ip_management_middleware.py`，只负责在请求进入路由前调用 IP 管理服务、写入 `request.state.ip_management`，并按作用域返回外部或内部错误响应。
- 应用服务：`app/services/ip_management_service.py` 负责设置缓存、作用域判断、规则匹配、限流编排、概览汇总和序列化。
- 解析服务：`app/services/ip_management_resolver_service.py` 负责直连 IP、可信代理 CIDR、转发头顺序和审计摘要解析。
- 规则服务：`app/services/ip_management_rule_service.py` 负责规则字段校验、匹配值归一化、优先级和精确度排序。
- 限流服务：`app/services/ip_management_rate_limit_service.py` 负责 IP 级 QPS/RPM 共享限流。
- 事件服务：`app/services/ip_management_event_service.py` 负责事件采样、落库、过期清理、请求日志回填和事件查询。
- 管理 API：`app/routers/ip_management.py` 只暴露设置、规则、事件、解析测试和规则测试接口，禁止其它模块绕过服务层直接拼装规则或事件口径。
- 前端页面：`app/templates/ip_management.html` 与 `app/static/js/app.js` 的 `initIpManagementPage` 负责设置、规则、事件和测试界面。

## 覆盖范围

IP 管理通过全局 `IpManagementMiddleware` 生效，并由 `IpManagementService.resolve_scope()` 将路径划分为：

| 作用域 | 路径范围 | 用途 |
| --- | --- | --- |
| `external_v1` | `/v1`、`/v1/*`、`/v1beta`、`/v1beta/*` | 外部模型代理入口，覆盖 OpenAI 兼容 Chat、Responses、Completions、Embeddings、Moderations、Files、Images、Models，以及 Gemini `/v1beta/models/*` 和 Claude `/v1/messages` 原生兼容入口。 |
| `internal_api` | `/api`、`/api/*` | 后台管理 API。 |
| `user_pages` | `/user`、`/user/*` | 用户端页面与用户端接口。 |

Gemini、Claude 等原生协议可以暴露独立外部入口；Gemini 使用 `/v1beta/models/{model}:generateContent` 与 `/v1beta/models/{model}:streamGenerateContent`，Claude 使用 `/v1/messages`。这些入口仍归入 `external_v1` 作用域，因此 IP 管理对所有模型请求过程的覆盖发生在路由和协议适配之前，不依赖具体模型分组或上游端点协议。

静态资源、登录页和其它未列入作用域的页面默认不由 IP 管理处理，避免影响无关页面和公共资源。

## 默认关闭与错误响应

- `IpManagementSetting` 默认关闭模块能力；关闭时中间件直接放行，不能改变 API Key 鉴权、请求日志、路由转发或历史来源 IP 白名单行为。
- 外部模型代理请求被阻断、限流或安全失败时，必须返回结构化错误对象，并带 `trace_id`；OpenAI 兼容入口保持 OpenAI 兼容 `error` 对象，原生协议入口按入口协议兼容性返回。
- 内部 `/api/*` 和 `/user/*` 请求被处置时，返回内部 JSON 错误，不包装成 OpenAI 错误。
- `fail_open_enabled` 控制模块异常时是否放行；关闭 fail-open 时，解析或限流状态不可用会返回结构化失败。

## 可信代理解析

- 默认只使用应用看到的直连 IP。
- 只有直连 IP 命中管理员配置的可信代理 CIDR 时，才允许读取 `X-Forwarded-For`、`Forwarded`、`CF-Connecting-IP` 等转发头。
- 解析结果必须保留 `resolution_source`、`resolution_status`、`trusted_proxy_matched`、代理链摘要、忽略头和告警，便于审计伪造转发头或代理链异常。
- 未命中可信代理时，转发头必须被忽略，不得用于安全判断。

## 规则、限流与事件

- 规则支持精确 IP、CIDR 和 IP 范围，按优先级升序匹配；同优先级下更具体的规则优先。
- 规则动作包括放行、记录、限流和阻断。观察模式开启时只记录命中，不执行阻断或限流。
- 事件记录受 `event_logging_enabled`、`event_sample_rate`、`ip_masking_enabled` 和 `store_raw_headers_enabled` 控制。
- 事件保留天数可由前端在 1-7 d 内调整；后端必须强制校验并归一化历史配置，禁止 IP 管理事件日志超过 7 天或永久保留。
- 事件查询支持关键词、解析 IP、作用域、动作、状态码、API Key 前缀、请求日志 ID、用户 ID 和时间范围。

## 请求日志关联

IP 管理事件创建后会通过 `proxy_request_context` 保存当前事件 ID。请求进入日志链路后：

- 同步日志由 `LogService.create_log()` 回填 `request_log_id`、API Key 前缀和用户 ID。
- 异步日志由 `RequestLogQueueService.enqueue()` 携带事件 ID，批量落库后回填事件上下文。
- 若请求在 IP 管理中间件阶段已被阻断，不一定存在请求日志；此时 IP 管理事件本身就是审计事实来源。

`trace_and_runtime_middleware` 不得在 `request.state.ip_management` 已存在时清空当前 IP 事件上下文，否则会导致请求日志无法反向关联 IP 管理事件。

## 前端能力

IP 管理页面包含四类工作区：

- 设置：模块启停、可信代理、作用域、规则引擎、阻断、限流、事件采样、保留天数和 fail-open。
- 规则：规则列表、筛选、新增、编辑、启停、删除。
- 事件：事件列表、日志关联字段、详情 JSON、过期清理和多维筛选。
- 测试：可信代理解析测试和规则命中测试。

所有帮助说明通过全站统一 Tooltip 组件展示，事件详情同时提供摘要区和原始 JSON，便于从请求日志、API Key 或用户维度追溯。

## 扩展约束

- 新增作用域必须先扩展 `resolve_scope()`、设置字段、前端筛选项、规则校验和文档。
- 新增规则动作必须同步扩展规则服务、事件序列化、错误响应策略、前端标签和测试。
- 新增日志关联字段必须通过 `IpManagementEventService.attach_request_context()` 或等价服务层方法回填，禁止其它模块直接写事件表。
- 新增安全判断不得直接信任客户端可伪造头，必须复用 `ClientIpResolver`。
