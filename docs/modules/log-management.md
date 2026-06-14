# 日志中心删除能力

摘要：日志中心负责后台请求日志、异常事件、健康检查日志、计费日志、内容防护日志、调度任务日志、后台审计日志、用户操作日志、告警事件和素材日志的查询、导出、详情查看与按筛选删除。关键词：request_logs、typed_logs、delete_filtered_logs、start_at、end_at、admin_audit。

## 职责边界

- 请求日志继续使用 `request_logs` 主表和 `LogService` 作为查询、导出和删除的统一入口。
- 类型化日志继续使用 `/api/logging/*` 路由内已有模型与筛选语义，不新增第二套日志模块。
- 删除操作只允许管理员在后台日志中心触发，用户端日志页面保持只读。
- 删除操作必须至少包含一个明确筛选条件或时间范围；仅默认排除健康检查不构成删除范围。
- 删除请求日志不级联删除类型化子事件；各类型化日志可在对应标签页按筛选独立删除。

## 管理接口

### 删除请求日志

- 路径：`DELETE /api/logs/filtered`
- 调用方：后台日志中心请求日志标签页。
- 核心参数：`log_type`、`provider_id`、`provider_trust_level`、`model_name`、`model_query`、`user_account_id`、`api_client_key_id`、`success`、`content_guard_*`、`conversation_key`、`tenant_name`、`project_name`、`app_name`、`environment_name`、`start_at`、`end_at`、`exclude_health_checks`。
- 行为：等待请求日志队列短暂空闲后，按当前筛选批量删除 `RequestLog`，并写入后台审计 `delete_filtered_logs`。
- 安全约束：没有筛选条件或时间范围时返回 `400`。

### 删除类型化日志

- 路径：`DELETE /api/logging/typed-events/{typed_log_type}`
- 调用方：后台日志中心类型化日志标签页。
- 支持类型：`exceptions`、`health-runs`、`billing-events`、`content-guard-events`、`background-jobs`、`admin-audits`、`user-operations`、`alert-events`、`asset-events`。
- 核心参数：复用对应列表/导出接口的筛选字段，包括 `keyword`、`start_at`、`end_at` 以及各类型专属字段。
- 特殊行为：健康检查日志删除 `HealthCheckRun` 时同步删除同批次 `HealthProbeEvent`；计费日志按 `TokenFinalizeEvent` 与 `BillingProcessEvent` 分别删除；内容防护日志通过 `RequestContentGuardEvent` 与 `RequestLog` 关联筛选后删除内容防护事件；后台任务日志只删除数据库日志，不删除 Redis 中的实时运行态。
- 审计：所有类型化日志删除均写入后台审计 `delete_typed_logs`，记录日志类型、筛选条件和底层删除数量。

## 前端交互

- 请求日志高级筛选增加 `开始时间` 与 `结束时间`。
- 请求日志工具栏提供 `删除筛选`，用于删除当前筛选命中的请求日志；保留原有 `清空请求日志` 兼容入口。
- 类型化日志工具栏提供 `删除筛选`，使用当前标签页的关键词、时间范围和专属筛选项。
- 前端会在空筛选时先阻止删除并提示；后端仍保留相同安全校验作为最终兜底。
