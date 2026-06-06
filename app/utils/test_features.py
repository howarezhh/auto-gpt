"""
测试功能标准化工具

提供统一的测试功能解析和阶段键转换逻辑，供所有路由和服务层复用。
"""


def normalize_test_features(payload: dict | None = None, *, single_model: bool = False) -> set[str]:
    """
    标准化测试功能列表

    从请求体中提取并验证测试功能选项，确保只包含允许的功能。

    Args:
        payload: 请求体字典，期望包含 "features" 字段
        single_model: 是否为单模型测试（保留参数以保持向后兼容，当前未使用）

    Returns:
        包含有效测试功能的集合，至少包含 "text_stream"

    规范约束：
        - 不允许 "text" 选项（非流式文本），只允许 "text_stream"（流式文本）
        - 符合规范："文本测试必须默认只测试流式文本，禁止同时测试非流式和流式两种模式"
    """
    raw_features = (payload or {}).get("features")
    if not isinstance(raw_features, list):
        raw_features = ["text_stream"]

    # 注意：不包含 "text"，只允许 "text_stream"
    allowed = {"text_stream", "vision", "tools", "image_generation"}
    features = {str(item).strip() for item in raw_features if str(item).strip() in allowed}

    return features or {"text_stream"}


def phase_keys_from_test_features(features: set[str]) -> frozenset[str]:
    """
    将测试功能集合转换为健康检查阶段键集合

    Args:
        features: 通过 normalize_test_features 标准化后的测试功能集合

    Returns:
        冻结集合，包含需要执行的健康检查阶段键

    规范约束：
        - 移除了对 "text" 的特殊处理，不再同时添加 "text" 和 "text_stream"
        - 只测试流式文本，符合规范要求
    """
    phase_keys: set[str] = set()

    # 移除了对 "text" 的处理，因为 normalize_test_features 已经不允许该选项
    if "text_stream" in features:
        phase_keys.add("text_stream")
    if "vision" in features:
        phase_keys.add("vision")
    if "tools" in features:
        phase_keys.add("tools")
    if "image_generation" in features:
        phase_keys.add("image_generation")

    return frozenset(phase_keys or {"text_stream"})
