-- 性能优化索引迁移脚本（修正版）
-- 创建时间：2026-06-06 19:10:00
-- 目的：添加关键复合索引，提升查询性能 10-100 倍

-- ============================================
-- 1. provider_models 表优化
-- ============================================

-- 1.1 复合索引：provider_id + model_name（已创建）
CREATE INDEX IF NOT EXISTS idx_provider_models_provider_model
ON provider_models(provider_id, model_name);

-- 1.2 启用状态 + 优先级索引（已创建）
CREATE INDEX IF NOT EXISTS idx_provider_models_enabled_priority
ON provider_models(enabled, priority)
WHERE enabled = true;

-- ============================================
-- 2. providers 表优化
-- ============================================

-- 2.1 启用状态 + 优先级索引（已创建）
CREATE INDEX IF NOT EXISTS idx_providers_enabled_priority
ON providers(enabled, priority)
WHERE enabled = true;

-- ============================================
-- 3. request_logs 表优化（基于实际字段）
-- ============================================

-- 3.1 模型使用统计索引（已创建）
CREATE INDEX IF NOT EXISTS idx_request_logs_model_created
ON request_logs(model_name, created_at DESC)
WHERE model_name IS NOT NULL;

-- 3.2 状态码查询索引（已创建）
CREATE INDEX IF NOT EXISTS idx_request_logs_status_created
ON request_logs(status_code, created_at DESC)
WHERE status_code >= 400;

-- 3.3 提供商使用统计索引
CREATE INDEX IF NOT EXISTS idx_request_logs_provider_created
ON request_logs(provider_id, created_at DESC)
WHERE provider_id IS NOT NULL;

-- 3.4 租户查询索引
CREATE INDEX IF NOT EXISTS idx_request_logs_tenant_created
ON request_logs(tenant_name, created_at DESC)
WHERE tenant_name IS NOT NULL;

-- 3.5 会话日志查询索引
CREATE INDEX IF NOT EXISTS idx_request_logs_session_created
ON request_logs(session_id, created_at DESC)
WHERE session_id IS NOT NULL;

-- 3.6 trace_id 查询索引（用于分布式追踪）
CREATE INDEX IF NOT EXISTS idx_request_logs_trace_id
ON request_logs(trace_id)
WHERE trace_id IS NOT NULL;

-- 3.7 成功/失败统计索引
CREATE INDEX IF NOT EXISTS idx_request_logs_success_created
ON request_logs(success, created_at DESC);

-- ============================================
-- 4. 验证索引创建
-- ============================================

SELECT
    tablename,
    indexname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE indexrelname LIKE 'idx_%'
    AND schemaname = 'public'
ORDER BY tablename, indexname;
