-- 迁移说明：为 provider_models 表添加原生协议自定义接口路径字段
-- 创建时间：2026-06-12
-- 变更原因：端点协议与原生接口路径属于具体提供商模型挂载，而不是提供商基础属性

ALTER TABLE provider_models
ADD COLUMN IF NOT EXISTS native_endpoint_path TEXT;

COMMENT ON COLUMN provider_models.native_endpoint_path IS '模型挂载级 Gemini/Claude 原生协议自定义接口路径模板；为空时使用官方默认路径或提供商历史兜底路径';
