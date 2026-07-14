"""
后端信号（BackendSignal）

职责：
- 执行 acli log/service/vm/network 等后端诊断指令
- 对命令输出进行关键字匹配判定
- 返回布尔判定结果与证据链

变量依赖（消费者角色）：
- target.scope 可引用 {{HOST}}（来自 FrontendSignal 提取）
- target.time_window 可引用 {{END}}（来自 FrontendSignal 提取）

@deprecated 运行时消费者统一由 signals_json 的 acquirer=qfk.* + matcher 描述，
并由 kbd_differential._execute_acquirer 路由到 qfk 引擎（§6 五类定型 valuator）。
本类保留仅作遗留/测试路径；qfk 引擎实际消费的模型在 app/tools/qfk/signal.py。
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.tools.signal.base import KeySignal, SignalCategory

if TYPE_CHECKING:
    from app.tools.qfk.engine import QFKResult


class BackendSignalType(StrEnum):
    """后端信号类型"""

    LOG_KEYWORD = "log_keyword"          # 日志关键字匹配
    SERVICE_STATUS = "service_status"    # 服务运行状态匹配
    VM_STATE = "vm_state"                # 虚拟机状态匹配
    NETWORK_CHECK = "network_check"      # 网络检查
    STORAGE_STATE = "storage_state"      # 存储状态匹配
    HARDWARE_STATE = "hardware_state"    # 硬件状态匹配
    PLATFORM_STATE = "platform_state"    # 平台状态匹配
    SYSTEM_METRIC = "system_metric"      # 系统指标匹配


class BackendSignalTarget(BaseModel):
    """
    后端信号目标定位（支持模板占位符）

    示例：
    - scope: "{{HOST}}" → 运行时渲染为 "node-001"
    - time_window: "{{END}}" → 运行时渲染为 "2026-07-09 10:00:00"
    """

    scope: str | None = Field(
        default=None,
        description="执行节点范围（可使用 ${variable} 占位符）"
    )
    resource: str | None = Field(
        default=None,
        description="资源名称（日志文件/服务名）"
    )
    path: str | None = Field(
        default=None,
        description="日志路径（仅 log_keyword 类型）"
    )
    time_window: str | None = Field(
        default=None,
        description="时间窗口（可使用 {{END}} 占位符）"
    )


class BackendSignal(KeySignal):
    """
    后端信号：运行时健康度判定

    典型流程：
    1. 从 KBD/SOP 文本提取（如"在备节点检查 mysql-managed.log 是否有只读错误"）
    2. 通过变量池渲染模板：target.scope="{{HOST}}" → "node-001"
    3. 执行 acli log get -k "file system read-only" -f mysql-managed.log
    4. 匹配关键字判定，返回 matched=True + evidence

    变量消费（消费者角色）：
    - 从变量池读取前端信号提取的变量值
    - 渲染 target 中的模板占位符
    """

    signal_category: SignalCategory = SignalCategory.BACKEND
    signal_type: BackendSignalType = Field(
        ...,
        description="后端信号类型"
    )
    target: BackendSignalTarget | None = Field(
        default=None,
        description="目标定位（可包含模板占位符）"
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="判定关键字列表"
    )
    match_mode: str = Field(
        default="any",
        description="匹配模式：any（或）/ all（与）"
    )
    expected: bool = Field(
        default=True,
        description="期望判定结果：True=期望出现，False=期望不出现"
    )
    container: str | None = Field(
        default=None,
        description="服务容器类型（仅 service_status 类型）：asv/anet/host"
    )
    sub_command: str | None = Field(
        default=None,
        description="子命令（仅 vm/network/storage 等类型）"
    )

    def extract(self) -> dict[str, Any]:
        """
        提取后端信号配置

        Returns:
            包含 signal_type, target, keywords 等的字典
        """
        return {
            "signal_category": self.signal_category.value,
            "signal_type": self.signal_type.value,
            "target": self.target.model_dump() if self.target else None,
            "keywords": self.keywords,
            "match_mode": self.match_mode,
            "expected": self.expected,
            "description": self.description,
            "container": self.container,
            "sub_command": self.sub_command,
        }

    def validate(self) -> tuple[bool, str | None]:
        """
        校验后端信号参数完整性

        Returns:
            (is_valid, error_message)
        """
        if not self.keywords:
            return False, "关键字列表不能为空"

        if self.match_mode not in ("any", "all"):
            return False, f"无效的匹配模式: {self.match_mode}，必须是 'any' 或 'all'"

        # 服务状态信号必须指定容器类型
        if (
            self.signal_type == BackendSignalType.SERVICE_STATUS
            and self.container
            and self.container not in ("asv", "anet", "host")
        ):
            return False, f"无效的容器类型: {self.container}"

        return True, None

    async def execute(
        self,
        conversation_id: str,
        node_ip: str | None = None,
        exec_id: str | None = None,
    ) -> QFKResult:
        """
        执行后端信号判定（调用 QFK 引擎）

        Args:
            conversation_id: 会话标识
            node_ip: 执行目标节点 IP（可选）
            exec_id: 流水号追踪（可选）

        Returns:
            QFKResult: 包含布尔判定结果与证据链
        """
        from app.tools.qfk.engine import qfk_exec

        return await qfk_exec(
            signal=self,
            conversation_id=conversation_id,
            node_ip=node_ip,
            exec_id=exec_id,
        )


