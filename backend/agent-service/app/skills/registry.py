"""迁移期本地 Skill 注册表。

生产运行时的 Skill 权威来源是数据库 skill_definition 表。
本模块只保留通用注册表能力，供极少数测试或迁移期适配使用。
"""

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# 全局技能注册表
_SKILL_REGISTRY: dict[str, Callable[[dict[str, Any]], Any]] = {}


def register_skill(name: str) -> Callable:
    """技能注册装饰器"""

    def decorator(func: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        _SKILL_REGISTRY[name] = func
        return func

    return decorator


async def execute_skill(skill_name: str, context_variables: dict[str, Any]) -> Any:
    """执行本地注册技能。生产链路应优先使用 DynamicSkillRunner。"""
    if skill_name not in _SKILL_REGISTRY:
        raise ValueError(f"未注册的技能: {skill_name}")

    func = _SKILL_REGISTRY[skill_name]
    logger.info(f"开始执行技能: {skill_name}")
    try:
        # 兼容同步与异步技能执行
        if callable(func):
            import inspect

            if inspect.iscoroutinefunction(func):
                result = await func(context_variables)
            else:
                result = func(context_variables)
            logger.info(f"技能 {skill_name} 执行成功，结果为: {result}")
            return result
        else:
            raise ValueError(f"技能 {skill_name} 的实现不是有效的函数")
    except Exception as exc:
        logger.error(f"技能 {skill_name} 执行失败: {exc}", exc_info=True)
        raise exc
