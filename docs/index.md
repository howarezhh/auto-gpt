# 工程文档索引

最后维护时间：2026-06-13

## 模块文档

| 文档名称 | 路径 | 所属模块 | 主要内容 | 关键词 | 关联代码目录 |
| --- | --- | --- | --- | --- | --- |
| 提供商原生协议与模型分组 | `docs/modules/provider-native-protocols.md` | 提供商管理、模型挂载、代理转发、健康检测、内容防护 | Gemini 与 Claude 原生协议适配、默认端点协议矩阵、模型分组规则、健康/可信/端点协议探针边界、日志字段 | provider、model_group、protocol_type、gemini、claude_messages、health_probe、content_guard_probe | `app/services/provider_service.py`、`app/services/proxy_service.py`、`app/services/health_service.py`、`app/services/content_guard_probe_service.py`、`app/static/js/app.js` |
