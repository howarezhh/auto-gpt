## [LRN-20260612-001] correction

**Logged**: 2026-06-12T00:00:00+08:00
**Priority**: medium
**Status**: pending
**Area**: backend

### Summary
固定答案探针不能使用包含式宽松匹配，只能在有限归一化后做严格相等判断。

### Details
用户明确纠正：SSE 固定答案检测不应因为模型输出包含固定答案且附加少量解释就判定通过。允许处理的范围应限制在首尾空白、无意义包装符号、标点、零宽字符、JSON 字符串或单一固定字段 JSON 外壳等可解释归一化；归一化后仍必须与固定答案完全相等。

### Suggested Action
后续新增或修改固定答案、哨兵字符串、完整性标记类探针时，复用“有限归一化后严格相等”的判定方式，禁止使用包含、正则命中或低风险关键词排除来替代精确比较。

### Metadata
- Source: user_feedback
- Related Files: app/services/content_guard_probe_service.py, tests/test_content_guard_regression.py
- Tags: content-guard, fixed-answer, sse, strict-match

---
