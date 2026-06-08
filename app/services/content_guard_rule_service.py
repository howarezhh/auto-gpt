from __future__ import annotations

from app.services.content_guard_service import ContentGuardService


class ContentGuardRuleService(ContentGuardService):
    """内容防护规则与算法入口。

    现阶段复用原 ContentGuardService 的确定性规则引擎，后续新增规则注册、风险评分或算法扩展时统一落在本服务。
    """

