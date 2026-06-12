-- 迁移说明：为 providers 表添加原生协议自定义接口路径字段
-- 创建时间：2026-06-12
-- 变更原因：Gemini/Claude 原生协议默认使用官方路径，同时允许第三方网关或自建服务配置路径模板

ALTER TABLE providers
ADD COLUMN IF NOT EXISTS native_endpoint_path TEXT;

COMMENT ON COLUMN providers.native_endpoint_path IS 'Gemini/Claude 原生协议自定义接口路径模板；为空时使用官方默认路径，支持 {model}、{raw_model}、{action} 占位符';
