"""
变量池 — Agent 工作记忆的 JIT 变量获取引擎（T-AGT-25）

通过微内核设计，实现“控制与执行解耦”：
- pool.py: 存储与状态核心（State Plane）
- engine.py: 策略决策内核（Control Plane）
"""

from app.memory.variable_pool.engine import sop_request_variable
from app.memory.variable_pool.pool import VariableRequestResult

__all__ = ["VariableRequestResult", "sop_request_variable"]
