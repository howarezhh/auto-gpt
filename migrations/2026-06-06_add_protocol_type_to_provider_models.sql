-- 迁移说明：为 provider_models 表添加 protocol_type 字段
-- 创建时间：2026-06-06 18:05:38
-- 变更原因：协议字段从中转站级移到模型挂载矩阵级，支持同一中转站下不同模型配置不同端点协议

-- 添加 protocol_type 字段，默认值为 'responses'
ALTER TABLE provider_models
ADD COLUMN IF NOT EXISTS protocol_type TEXT NOT NULL DEFAULT 'responses';

-- 添加注释说明
COMMENT ON COLUMN provider_models.protocol_type IS '模型挂载级端点协议类型：responses、chat_completions 或 both';

-- 验证字段已添加
-- SELECT column_name, data_type, column_default, is_nullable
-- FROM information_schema.columns
-- WHERE table_name = 'provider_models' AND column_name = 'protocol_type';
