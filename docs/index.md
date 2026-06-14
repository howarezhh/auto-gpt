# 工程文档索引

最后维护时间：2026-06-14

## 模块文档

| 文档名称 | 路径 | 所属模块 | 主要内容 | 关键词 | 关联代码目录 |
| --- | --- | --- | --- | --- | --- |
| 计费模块 | `docs/modules/billing.md` | 计费、价格、余额、用量统计、账单审计 | 原币种计费、多币种汇率快照、缓存写入价、非 token 费用组件、余额预占、未定价拦截、后台用户额度用量、前端计费展示契约 | billing、source_currency、billing_currency、exchange_rate_snapshot、cache_write_price_per_1k、fee_components、reservation、quota | `app/services/billing_service.py`、`app/services/model_pricing_service.py`、`app/services/billing_reservation_service.py`、`app/services/proxy_service.py`、`app/services/user_portal_service.py`、`app/services/model_catalog_service.py`、`app/static/js/app.js`、`app/templates/users.html`、`app/templates/models.html`、`app/templates/user_billing.html`、`app/templates/user_models.html` |
| 提供商原生协议与模型分组 | `docs/modules/provider-native-protocols.md` | 提供商管理、模型挂载、批量导入、代理转发、健康检测、内容防护 | 模型别名与上游模型 ID、Gemini 与 Claude 原生协议适配、默认端点协议矩阵、模型分组规则、批量导入字段规则、模型聚合列表分组筛选、健康/可信/端点协议探针边界、日志字段 | provider、model_name、upstream_model_name、model_group、protocol_type、gemini、claude_messages、batch_import、health_probe、content_guard_probe | `app/services/provider_service.py`、`app/services/proxy_service.py`、`app/services/health_service.py`、`app/services/content_guard_probe_service.py`、`app/static/js/app.js` |
| 日志中心删除能力 | `docs/modules/log-management.md` | 日志中心、请求日志、类型化日志、后台审计 | 请求日志与类型化日志按时间范围或当前筛选删除、删除接口、安全范围校验、审计记录、前端入口 | request_logs、typed_logs、delete_filtered_logs、delete_typed_logs、start_at、end_at、admin_audit | `app/routers/logs.py`、`app/routers/logging_api.py`、`app/services/log_service.py`、`app/templates/logs.html`、`app/static/js/app.js` |
| IP 管理模块 | `docs/modules/ip-management.md` | IP 管理、可信代理、IP 规则、IP 限流、处置事件、请求日志关联 | 模块边界、作用域覆盖、默认关闭、可信代理解析、规则匹配、事件日志、请求日志回填、前端工作区和扩展约束 | IpManagementMiddleware、ClientIpResolver、ip_access_rules、ip_management_events、external_v1、internal_api、user_pages、request_log_id | `app/middleware/ip_management_middleware.py`、`app/services/ip_management_service.py`、`app/services/ip_management_resolver_service.py`、`app/services/ip_management_rule_service.py`、`app/services/ip_management_event_service.py`、`app/routers/ip_management.py`、`app/templates/ip_management.html`、`app/static/js/app.js` |
