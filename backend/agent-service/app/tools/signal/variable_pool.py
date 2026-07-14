"""
变量池管理器（VariablePool）

职责：
- 管理会话级变量存储
- 从前端信号执行结果中注册变量
- 为后端信号渲染模板占位符
- 实现生产者-消费者模式的变量流转
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shared.observability.logger import get_logger

if TYPE_CHECKING:
    from app.tools.qkv.engine import QKVResult
    from app.tools.signal.backend import BackendSignal

logger = get_logger("variable-pool")


class VariablePool:
    """
    会话级变量池（管理信号间的变量流转）

    核心模式：
    - FrontendSignal 作为生产者，执行后将变量写入变量池
    - BackendSignal 作为消费者，执行前从变量池读取变量渲染模板

        示例流程：
        1. FrontendSignal 执行 → 提取 HOST="node-001" → 写入变量池
        2. BackendSignal(target.scope="{{HOST}}") → 变量池渲染 → target.scope="node-001"
        """

    def __init__(self, conversation_id: str):
        """
        初始化变量池

        Args:
            conversation_id: 会话标识
        """
        self.conversation_id = conversation_id
        self._variables: dict[str, Any] = {}

    def register(self, key: str, value: Any) -> None:
        """
        注册变量到变量池

        Args:
            key: 变量名（ADR-2：全大写，如 "HOST", "VM", "END"）
            value: 变量值
        """
        self._variables[key] = value
        logger.info(
            "variable_registered",
            conversation_id=self.conversation_id,
            key=key,
            value=str(value)[:100],
        )

    def register_from_frontend_result(self, result: QKVResult) -> None:
        """
        从前端信号执行结果中批量注册变量

        Args:
            result: QKV 执行结果

        流程：
        1. QKV 执行完成，返回 QKVResult
        2. 提取 values 中的 host, vm, end, target 等字段
        3. 键名归一为全大写后写入变量池（ADR-2：占位符必须 {{大写}}）

        示例：
        - result.values[0]["host"] = "node-001" → 变量池["HOST"] = "node-001"
        - result.values[0]["end"] = "2026-07-09 10:00:00" → 变量池["END"] = "..."
        """
        if not result.success or not result.values:
            logger.warning(
                "frontend_result_empty",
                conversation_id=self.conversation_id,
                success=result.success,
            )
            return

        # 取第一条记录的关键字段
        first_value = result.values[0]

        # 定义需要提取的变量列表（注册时统一转全大写，对齐 ADR-2 占位符 {{VAR}}）
        variable_keys = [
            "host",
            "vm",
            "end",
            "target",
            "trace_id",
            "errcode_tracing",
            "request_id",
            "alert_type",
            "description",
        ]

        for key in variable_keys:
            value = first_value.get(key)
            if value and str(value).strip():
                self.register(key.upper(), value)

        logger.info(
            "variables_registered_from_frontend",
            conversation_id=self.conversation_id,
            count=len([k for k in variable_keys if first_value.get(k)]),
        )

    def get(self, key: str) -> Any | None:
        """
        从变量池获取变量值

        Args:
            key: 变量名

        Returns:
            变量值，不存在时返回 None
        """
        return self._variables.get(key)

    def render_template(self, template_value: str) -> str:
        """
        渲染模板字符串中的占位符

        ADR-2（强制）：占位符统一为 {{VAR}}（全大写双花括号）。旧式 ${VAR} / {VAR}
        不被识别，保持原样不渲染——以此强制信号模板遵循 {{大写}} 规范，避免小写/
        单花括号占位符被静默忽略导致变量未注入。占位符命名合法性（全大写）的强制
        校验在信号抽取期由 `validate_placeholder_case` 完成（接入点：extract_signals）。

        Args:
            template_value: 包含 {{VARIABLE}} 占位符（全大写）的字符串

        Returns:
            渲染后的字符串（未注册的占位符保持原样）

        示例：
        - "{{HOST}}" → "node-001"
        - "prefix-{{HOST}}-suffix" → "prefix-node-001-suffix"
        - "plain-text" → "plain-text"
        - "${host}" / "{host}" → 保持原样（不被识别为占位符）
        """
        if not isinstance(template_value, str):
            return template_value

        # 纯占位符 {{VARIABLE}}
        if len(template_value) >= 4 and template_value.startswith("{{") and template_value.endswith("}}"):
            var_name = template_value[2:-2].strip()
            return self._variables.get(var_name, template_value)

        # 全局替换（仅认 {{VAR}} 全大写双花括号；${VAR} / {VAR} 旧式不渲染，强制 ADR-2）
        import re
        pattern = r"\{\{([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)\}\}"

        def replace(match):
            var_name = match.group(1)
            return str(self._variables.get(var_name, match.group(0)))

        return re.sub(pattern, replace, template_value)

    def render_backend_signal(self, signal: BackendSignal) -> BackendSignal:
        """
        渲染后端信号的模板占位符

        Args:
            signal: 原始后端信号实例

        Returns:
            渲染后的后端信号实例

        示例：
        - BackendSignal(target.scope="{{HOST}}") → BackendSignal(target.scope="node-001")
        """
        if not signal.target:
            return signal

        # 深拷贝信号对象
        signal_dict = signal.model_dump()

        # 渲染 target 中的所有字段
        if signal_dict.get("target"):
            rendered_target = {}
            for key, value in signal_dict["target"].items():
                if isinstance(value, str):
                    rendered_target[key] = self.render_template(value)
                else:
                    rendered_target[key] = value
            signal_dict["target"] = rendered_target

        # 重新构造信号实例
        from app.tools.signal.backend import BackendSignal
        return BackendSignal.from_dict(signal_dict)

    def to_dict(self) -> dict[str, Any]:
        """导出变量池内容为字典"""
        return self._variables.copy()

    def clear(self) -> None:
        """清空变量池"""
        self._variables.clear()
        logger.info("variable_pool_cleared", conversation_id=self.conversation_id)
