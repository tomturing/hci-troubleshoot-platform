"""
变量池 — 存储与状态核心（State Plane）

定义 JIT 变量请求的返回结果数据模型，负责工作记忆的结构表达。
"""

from typing import Any


class VariableRequestResult:
    """变量请求结果（用于标识需要阻塞等待用户输入）。

    当 sop_request_variable 需要用户输入时返回此类型，
    ReactEngine 或 InvestigationAgent 捕获此结果后 yield AgentInteractiveRequest。

    Attributes:
        needs_input: 是否需要用户输入（True 时阻塞等待）
        variable_name: 变量名
        variable_schema: 变量 Schema 定义（含 display_name、description、validation_pattern）
        current_value: 当前值（若已存在）
        message: 消息（用于 LLM 或用户）
        kind: 交互类型（variable_input / variable_confirm）
        options: 候选选项列表（user_confirm 类型时使用）
    """

    def __init__(
        self,
        *,
        needs_input: bool = False,
        variable_name: str = "",
        variable_schema: dict | None = None,
        current_value: Any = None,
        message: str = "",
        kind: str = "variable_input",
        options: list[dict] | None = None,
    ):
        self.needs_input = needs_input
        self.variable_name = variable_name
        self.variable_schema = variable_schema or {}
        self.current_value = current_value
        self.message = message
        self.kind = kind
        self.options = options or []
