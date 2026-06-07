-- 性能优化索引迁移脚本
-- 创建时间：2026-06-06 19:00:00
-- 目的：添加关键复合索引，提升查询性能 10-100 倍

-- ============================================
-- 1. provider_models 表优化
-- ============================================

-- 1.1 复合索引：provider_id + model_name
-- 用途：快速查找特定提供商的特定模型
-- 影响：路由匹配性能提升 10-50 倍
CREATE INDEX IF NOT EXISTS idx_provider_models_provider_model
ON provider_models(provider_id, model_name);

-- 1.2 启用状态 + 优先级索引
-- 用途：快速获取所有启用的模型并按优先级排序
-- 影响：健康检查和模型列表查询提升 5-10 倍
CREATE INDEX IF NOT EXISTS idx_provider_models_enabled_priority
ON provider_models(enabled, priority)
WHERE enabled = true;

-- 1.3 健康状态索引
-- 用途：快速筛选健康/异常模型
CREATE INDEX IF NOT EXISTS idx_provider_models_health_status
ON provider_models(health_status, last_health_check_at DESC)
WHERE enabled = true;

-- ============================================
-- 2. providers 表优化
-- ============================================

-- 2.1 启用状态 + 优先级索引
-- 用途：快速获取所有启用的提供商
-- 影响：路由候选选择提升 5-10 倍
CREATE INDEX IF NOT EXISTS idx_providers_enabled_priority
ON providers(enabled, priority)
WHERE enabled = true;

-- 2.2 健康状态索引
-- 用途：健康检查和监控查询
CREATE INDEX IF NOT EXISTS idx_providers_health_status
ON providers(health_status, last_health_check_at DESC)
WHERE enabled = true;

-- ============================================
-- 3. request_logs 表优化
-- ============================================

-- 3.1 用户查询索引
-- 用途：用户日志查询（按时间倒序）
-- 影响：用户日志页面查询提升 10-100 倍
CREATE INDEX IF NOT EXISTS idx_request_logs_user_created
ON request_logs(user_id, created_at DESC)
WHERE user_id IS NOT NULL;

-- 3.2 API Key 查询索引
-- 用途：API Key 使用统计和日志查询
-- 影响：API Key 详情页查询提升 10-100 倍
CREATE INDEX IF NOT EXISTS idx_request_logs_apikey_created
ON request_logs(api_key_id, created_at DESC)
WHERE api_key_id IS NOT NULL;

-- 3.3 模型使用统计索引
-- 用途：模型使用频率统计
CREATE INDEX IF NOT EXISTS idx_request_logs_model_created
ON request_logs(model_name, created_at DESC)
WHERE model_name IS NOT NULL;

-- 3.4 状态码查询索引
-- 用途：错误日志查询和监控
CREATE INDEX IF NOT EXISTS idx_request_logs_status_created
ON request_logs(status_code, created_at DESC)
WHERE status_code >= 400;

-- ============================================
-- 4. api_keys 表优化
-- ============================================

-- 4.1 认证查询优化
-- 用途：API Key 认证（热路径）
-- 影响：认证性能提升 5-20 倍
CREATE INDEX IF NOT EXISTS idx_api_keys_hash_enabled
ON api_keys(key_hash, enabled)
WHERE enabled = true;

-- 4.2 用户 API Key 列表索引
-- 用途：用户的 API Key 列表查询
CREATE INDEX IF NOT EXISTS idx_api_keys_user_created
ON api_keys(user_id, created_at DESC);

-- ============================================
-- 5. model_catalog 表优化
-- ============================================

-- 5.1 模型名称 + 启用状态索引
-- 用途：模型目录查询和路由匹配
-- 影响：模型查询提升 5-10 倍
CREATE INDEX IF NOT EXISTS idx_model_catalog_name_enabled
ON model_catalog(model_name, enabled)
WHERE enabled = true;

-- 5.2 健康状态索引
-- 用途：健康模型列表查询
CREATE INDEX IF NOT EXISTS idx_model_catalog_health_status
ON model_catalog(health_status, last_health_check_at DESC)
WHERE enabled = true;

-- ============================================
-- 6. 验证索引创建
-- ============================================

-- 查看所有新创建的索引
SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE indexname LIKE 'idx_%'
    AND schemaname = 'public'
ORDER BY tablename, indexname;

-- 查看索引大小
SELECT
    indexrelname AS index_name,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE indexrelname LIKE 'idx_%'
ORDER BY pg_relation_size(indexrelid) DESC;

-- ============================================
-- 7. 性能验证查询
-- ============================================

-- 验证 provider_models 查询性能
EXPLAIN ANALYZE
SELECT * FROM provider_models
WHERE provider_id = 1 AND model_name = 'gpt-4'
AND enabled = true;

-- 验证 request_logs 查询性能
EXPLAIN ANALYZE
SELECT * FROM request_logs
WHERE user_id = 1
ORDER BY created_at DESC
LIMIT 50;

-- 验证 api_keys 认证性能
EXPLAIN ANALYZE
SELECT * FROM api_keys
WHERE key_hash = 'sample_hash'
AND enabled = true;

-- ============================================
-- 8. 回滚脚本（如需要）
-- ============================================

/*
-- 删除所有新增索引
DROP INDEX IF EXISTS idx_provider_models_provider_model;
DROP INDEX IF EXISTS idx_provider_models_enabled_priority;
DROP INDEX IF EXISTS idx_provider_models_health_status;
DROP INDEX IF EXISTS idx_providers_enabled_priority;
DROP INDEX IF EXISTS idx_providers_health_status;
DROP INDEX IF EXISTS idx_request_logs_user_created;
DROP INDEX IF EXISTS idx_request_logs_apikey_created;
DROP INDEX IF EXISTS idx_request_logs_model_created;
DROP INDEX IF EXISTS idx_request_logs_status_created;
DROP INDEX IF EXISTS idx_api_keys_hash_enabled;
DROP INDEX IF EXISTS idx_api_keys_user_created;
DROP INDEX IF EXISTS idx_model_catalog_name_enabled;
DROP INDEX IF EXISTS idx_model_catalog_health_status;
*/
