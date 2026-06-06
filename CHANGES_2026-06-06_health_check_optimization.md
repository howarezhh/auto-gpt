# 健康检查优化与协议字段迁移 - 2026-06-06

## 变更概述

本次变更将协议字段从中转站级移到模型挂载矩阵级，并优化健康检查测试策略，显著缩短测试时间。

## 主要变更

### 1. 数据库模型修改 ✅

**文件**: `app/models/provider_model.py`

- 在 `provider_models` 表添加 `protocol_type` 字段
- 类型：TEXT，默认值：`"responses"`
- 支持的值：`responses`、`chat_completions`、`both`

### 2. Schema 修改 ✅

**文件**: `app/schemas/provider.py`

- `ProviderModelConfigBase` 添加 `protocol_type` 字段及验证器
- `ProviderModelConfigUpdate` 添加 `protocol_type` 字段支持
- `ProviderModelConfigOut` 包含 `protocol_type` 和 `protocol_label` 字段
- 复用现有的 `normalize_provider_protocol_type` 函数进行标准化

### 3. 健康检查服务优化 ✅

**文件**: `app/services/health_service.py`

#### 3.1 移除非流式文本测试
- **变更前**: 有 `text`（非流式）和 `text_stream`（流式）两个测试阶段
- **变更后**: 只保留 `text_stream`（流式文本测试）
- **原因**: 流式测试已覆盖文本能力，移除非流式测试可减少一半测试时间

#### 3.2 禁用重试机制
- **函数**: `_probe_with_retry`
- **变更前**: 失败后最多重试 2 次（`PROBE_RETRY_MAX_ATTEMPTS = 2`）
- **变更后**: 所有测试只执行一次，不重试
- **原因**: 避免失败重试延长测试时间

#### 3.3 智能端点选择
- **新增函数**:
  - `_get_model_protocol(model)`: 获取模型挂载级协议类型
  - `_should_test_endpoint(model, endpoint)`: 判断是否应该测试指定端点

- **选择逻辑**:
  - `both` 模式：优先测试 `responses`，provider 不支持时测试 `chat`
  - `responses` 模式：只测试 `responses` 端点
  - `chat_completions` 模式：只测试 `chat` 端点

- **应用范围**:
  - 流式文本测试
  - 工具调用测试
  - 图像理解测试

#### 3.4 性能提升效果
- **测试阶段减少**: 从 5 个阶段减少到 4 个阶段（移除非流式文本）
- **端点测试减少**: 每个能力从测试 2 个端点减少到只测 1 个端点
- **重试次数减少**: 从最多 2 次重试减少到 0 次重试
- **预计提升**: 总测试时间减少 60-70%

### 4. 项目全局规范更新 ✅

**文件**: `项目全局规范.md`

新增以下规范条目：
- 后台健康检查所有能力测试必须只执行一次，禁止失败后自动重试
- 文本测试必须默认只测试流式文本，禁止同时测试非流式和流式两种模式
- 中转站模型挂载矩阵的 `protocol_type` 字段作为端点协议权威来源
- 测试端点按挂载级协议选择，优先 `responses`，不支持时选择 `chat_completions`

### 5. 变更记录更新 ✅

**文件**: `项目全局规范-变更记录.md`

已记录本次变更的时间、类型、摘要和原因。

## 数据库迁移

### 迁移脚本

**文件**: `migrations/2026-06-06_add_protocol_type_to_provider_models.sql`

### 执行步骤

1. **备份数据库**（生产环境必须）
   ```bash
   pg_dump -h localhost -U postgres -d aotu_gpt > backup_2026-06-06.sql
   ```

2. **执行迁移**
   ```bash
   psql -h localhost -U postgres -d aotu_gpt -f migrations/2026-06-06_add_protocol_type_to_provider_models.sql
   ```

3. **验证迁移**
   ```sql
   SELECT column_name, data_type, column_default, is_nullable
   FROM information_schema.columns
   WHERE table_name = 'provider_models' AND column_name = 'protocol_type';
   ```

   预期结果：
   - column_name: `protocol_type`
   - data_type: `text`
   - column_default: `'responses'::text`
   - is_nullable: `NO`

## 前端修改（待完成）

### 需要修改的文件

**文件**: `app/static/js/app.js`

### 修改内容

1. **修改 `DEFAULT_PROVIDER_MODEL_CONFIG`**
   ```javascript
   const DEFAULT_PROVIDER_MODEL_CONFIG = {
       protocol_type: "responses",  // 新增
       supports_stream: true,
       supports_vision: true,
       supports_tools: true,
       enabled: true,
       price_multiplier: 1,
   };
   ```

2. **修改 `normalizeProviderModelConfig` 函数**
   ```javascript
   function normalizeProviderModelConfig(config = {}) {
       const modelName = String(config.model_name || "").trim();
       return {
           model_name: modelName,
           protocol_type: config.protocol_type || DEFAULT_PROVIDER_MODEL_CONFIG.protocol_type,  // 新增
           supports_stream: config.supports_stream ?? DEFAULT_PROVIDER_MODEL_CONFIG.supports_stream,
           supports_vision: config.supports_vision ?? DEFAULT_PROVIDER_MODEL_CONFIG.supports_vision,
           supports_tools: config.supports_tools ?? DEFAULT_PROVIDER_MODEL_CONFIG.supports_tools,
           enabled: config.enabled ?? DEFAULT_PROVIDER_MODEL_CONFIG.enabled,
           price_multiplier: Number.isFinite(Number(config.price_multiplier)) && Number(config.price_multiplier) > 0
               ? Number(config.price_multiplier)
               : DEFAULT_PROVIDER_MODEL_CONFIG.price_multiplier,
       };
   }
   ```

3. **修改 `createProviderModelConfigRow` 函数**

   在模型配置行中添加协议类型选择器：

   ```javascript
   row.innerHTML = `
       <label class="provider-model-config-name" for="${rowId}-name">
           <span class="visually-hidden">模型名称</span>
           <input class="field-input" id="${rowId}-name" data-model-config-field="model_name"
                  value="${escapeHtml(item.model_name)}" placeholder="模型名称（必填）" required>
       </label>
       <label class="provider-model-mini-switch settings-switch-control" title="控制该模型是否加入当前中转站路由">
           <input type="checkbox" data-model-config-field="enabled" ${item.enabled ? "checked" : ""}>
           <span class="settings-switch-slider" aria-hidden="true"></span>
       </label>
       <label>
           <span class="visually-hidden">协议类型</span>
           <select class="field-input" data-model-config-field="protocol_type">
               <option value="responses" ${item.protocol_type === "responses" ? "selected" : ""}>Responses API</option>
               <option value="chat_completions" ${item.protocol_type === "chat_completions" ? "selected" : ""}>Chat Completions API</option>
               <option value="both" ${item.protocol_type === "both" ? "selected" : ""}>双协议</option>
           </select>
       </label>
       <label>
           <span class="visually-hidden">价格倍率</span>
           <input class="field-input" type="number" min="0.0001" step="0.0001"
                  data-model-config-field="price_multiplier" value="${item.price_multiplier}" placeholder="倍率">
       </label>
       <button class="table-action-btn" data-action="remove-model-config" type="button">删除</button>
   `;
   ```

4. **修改 `collectProviderModelConfigs` 函数**

   在收集模型配置时包含 `protocol_type`：

   ```javascript
   const protocolTypeInput = row.querySelector('[data-model-config-field="protocol_type"]');
   const protocolType = protocolTypeInput?.value || DEFAULT_PROVIDER_MODEL_CONFIG.protocol_type;

   configs.push({
       model_name: modelName,
       protocol_type: protocolType,  // 新增
       supports_stream: row.dataset.supportsStream ? row.dataset.supportsStream === "true" : DEFAULT_PROVIDER_MODEL_CONFIG.supports_stream,
       supports_vision: row.dataset.supportsVision ? row.dataset.supportsVision === "true" : DEFAULT_PROVIDER_MODEL_CONFIG.supports_vision,
       supports_tools: row.dataset.supportsTools ? row.dataset.supportsTools === "true" : DEFAULT_PROVIDER_MODEL_CONFIG.supports_tools,
       enabled: row.querySelector('[data-model-config-field="enabled"]')?.checked ?? DEFAULT_PROVIDER_MODEL_CONFIG.enabled,
       price_multiplier: priceMultiplier,
   });
   ```

5. **中转站卡片协议字段处理**

   - **选项 A**: 从中转站编辑表单中完全移除协议类型选择（推荐）
   - **选项 B**: 将其改为只读显示，显示该中转站下所有模型的协议汇总

### CSS 样式调整（可选）

如需优化协议选择器的样式，可以在 `app/static/css/app.css` 中添加：

```css
.provider-model-config-row select[data-model-config-field="protocol_type"] {
    min-width: 150px;
}
```

## 测试验证

### 1. 后端测试

启动应用并测试健康检查：

```bash
# 启动应用
.\.venv\Scripts\activate
python -m uvicorn app.main:app --reload

# 测试健康检查 API
curl -X POST http://localhost:8000/api/providers/{provider_id}/check
```

验证点：
- [ ] 测试时间明显缩短（约 60-70% 减少）
- [ ] 只测试流式文本，不测试非流式文本
- [ ] 失败不重试
- [ ] 根据 `protocol_type` 选择测试端点

### 2. 前端测试（待前端修改完成后）

在浏览器中测试：

1. 打开中转站编辑页面
2. 添加新模型，验证协议类型下拉选择器
3. 保存后验证数据正确提交
4. 刷新页面验证数据正确回显

## 回滚方案

如需回滚数据库更改：

```sql
-- 移除 protocol_type 字段
ALTER TABLE provider_models DROP COLUMN IF EXISTS protocol_type;
```

## 后续优化建议

1. **前端界面优化**: 完成前端修改，将协议选项移到模型列表中
2. **批量更新工具**: 提供批量更新现有模型 `protocol_type` 的管理界面
3. **文档更新**: 更新用户使用文档，说明新的协议配置方式
4. **监控指标**: 添加健康检查耗时监控，验证性能提升效果

## 相关文件清单

### 已修改文件
- `app/models/provider_model.py`
- `app/schemas/provider.py`
- `app/services/health_service.py`
- `项目全局规范.md`
- `项目全局规范-变更记录.md`

### 新增文件
- `migrations/2026-06-06_add_protocol_type_to_provider_models.sql`
- `CHANGES_2026-06-06_health_check_optimization.md`（本文件）

### 待修改文件
- `app/static/js/app.js`（前端模型配置界面）
- `app/static/css/app.css`（可选样式调整）

---

**变更完成时间**: 2026-06-06 18:06
**变更负责人**: Claude Code
**审核状态**: 待用户确认
