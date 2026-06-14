"""
共享数据模型统一导出模块

设计原则：延迟导入（Lazy Import）
- SQLAlchemy ORM 类定义会在 import 时把表注册到 Base.metadata
- 包根自动导入所有子模块会产生不必要的 ORM 注册副作用
- 使用 __getattr__ 实现延迟导入：只在使用时才加载对应模块

用法：
- 包根导入：from shared.models import Conversation  # 只加载 conversation.py
- 直接导入：from shared.models.conversation import Conversation  # 同上，推荐
- 全量导入：from shared.models import *  # 触发所有模块加载（慎用）
"""

# 导出声明（不触发实际导入）
__all__ = [
    "User",
    "Conversation",
    "DynamicResourceActive",
    "DynamicResourceRevision",
    "DynamicResourceUsageAudit",
    "SystemPrompt",
    "PromptSlot",
    "AuditLog",
    "Authorization",
    "ToolResult",
    "KBDocument",
    "KBChunk",
    "KBSopNode",
    "KBCategory",
    "KBSynonym",
    "SkillDefinitionORM",
    "TimestampMixin",
    "TraceableMixin",
    # 阶段二：轻量事实体系
    "FactSource",
    "InformationPacket",
    "StaleDataGuard",
    "EvidenceBundle",
    # 阶段三：推理约束与反幻觉
    "Hypothesis",
    "ReasoningOutput",
    "Claim",
    "ClaimVerification",
    # 阶段四：事实持久化与评估
    "Fact",
    "ClaimEvidenceLink",
]

# 模型名 → 子模块名映射
_MODULE_MAP = {
    "User": "user",
    "Conversation": "conversation",
    "DynamicResourceActive": "dynamic_resource",
    "DynamicResourceRevision": "dynamic_resource",
    "DynamicResourceUsageAudit": "dynamic_resource",
    "PromptSlot": "dynamic_resource",
    "SystemPrompt": "system_prompt",
    "AuditLog": "audit",
    "Authorization": "audit",
    "ToolResult": "audit",
    "KBDocument": "kb",
    "KBChunk": "kb",
    "KBSopNode": "kb",
    "KBCategory": "kb",
    "KBSynonym": "kb",
    "SkillDefinitionORM": "skill_definition",
    "TimestampMixin": "base",
    "TraceableMixin": "base",
    "FactSource": "information",
    "InformationPacket": "information",
    "StaleDataGuard": "information",
    "EvidenceBundle": "information",
    "Hypothesis": "reliability",
    "ReasoningOutput": "reliability",
    "Claim": "reliability",
    "ClaimVerification": "reliability",
    "Fact": "fact",
    "ClaimEvidenceLink": "fact",
}


def __getattr__(name: str):
    """延迟导入：只在使用时才加载对应模块，避免 ORM 注册副作用

    当使用 from shared.models import X 时：
    1. 检查 X 是否在 __all__ 中
    2. 根据 _MODULE_MAP 找到对应的子模块
    3. 动态导入该子模块并获取 X
    4. 缓存到 globals() 避免重复导入

    这样 kb-service 导入 shared.models.dynamic_resource 时，
    不会触发 KB 相关 ORM 的注册，避免与 kb-service 内部模型冲突。
    """
    if name not in __all__:
        raise AttributeError(f"module {__name__} has no attribute {name}")

    module_name = _MODULE_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__} has no mapping for {name}")

    # 动态导入子模块
    import importlib
    module = importlib.import_module(f".{module_name}", __package__)

    # 获取目标类/函数
    if not hasattr(module, name):
        raise AttributeError(f"module shared.models.{module_name} has no attribute {name}")

    value = getattr(module, name)

    # 缓存到 globals，下次直接返回
    globals()[name] = value
    return value


def __dir__():
    """返回可导出的属性列表，支持 IDE 自动补全"""
    return __all__
