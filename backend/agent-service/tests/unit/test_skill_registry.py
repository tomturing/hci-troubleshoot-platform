"""迁移期本地 Skill 注册表单元测试。"""

import sys
from pathlib import Path

import pytest

# 隔离 agent-service app 命名空间
_svc = str(Path(__file__).resolve().parents[2])
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.skills import execute_skill, register_skill, registry


def test_register_skill_decorator():
    """register_skill 装饰器将函数注册到全局注册表"""

    @register_skill("test_skill")
    def test_func(ctx):
        return f"result: {ctx['key']}"

    assert "test_skill" in registry._SKILL_REGISTRY
    assert registry._SKILL_REGISTRY["test_skill"] is test_func
    # cleanup
    del registry._SKILL_REGISTRY["test_skill"]


@pytest.mark.asyncio
async def test_execute_skill_sync():
    """同步技能正常执行并返回结果"""

    @register_skill("sync_test")
    def sync_func(ctx):
        return ctx["input"] * 2

    result = await execute_skill("sync_test", {"input": 5})
    assert result == 10
    del registry._SKILL_REGISTRY["sync_test"]


@pytest.mark.asyncio
async def test_execute_skill_async():
    """异步技能正常执行并返回结果"""

    @register_skill("async_test")
    async def async_func(ctx):
        return ctx["input"] + 1

    result = await execute_skill("async_test", {"input": 9})
    assert result == 10
    del registry._SKILL_REGISTRY["async_test"]


@pytest.mark.asyncio
async def test_execute_skill_not_found():
    """执行未注册技能抛出 ValueError"""
    with pytest.raises(ValueError, match="未注册的技能"):
        await execute_skill("nonexistent_skill", {})


@pytest.mark.asyncio
async def test_execute_skill_propagates_exception():
    """技能执行中的异常正常向上传播"""

    def failing_func(ctx):
        raise RuntimeError("skill failed")

    registry._SKILL_REGISTRY["failing_skill"] = failing_func
    with pytest.raises(RuntimeError, match="skill failed"):
        await execute_skill("failing_skill", {})
    del registry._SKILL_REGISTRY["failing_skill"]
