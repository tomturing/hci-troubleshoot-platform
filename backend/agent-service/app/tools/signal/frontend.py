"""
前端信号（FrontendSignal）

职责：
- 通过 acli alert/task/log get 提取告警/任务/弹框数据
- 清洗并提取 host, vm, time, errcode 等元数据变量
- 将变量写入会话变量池供后端信号消费

@deprecated 运行时生产者统一由 signals_json 的 acquirer=qkv.* + produces 描述，
并由 kbd_differential._run_producers 调用 qkv 引擎执行（见 KeySignal 基类规范字段）。
本类保留仅作遗留/测试路径；qkv 引擎实际消费的模型在 app/tools/qkv/signal.py。
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import Field

from app.tools.signal.base import KeySignal, SignalCategory

if TYPE_CHECKING:
    from app.tools.qkv.engine import QKVResult


class FrontendQueryType(StrEnum):
    """前端信号查询类型"""

    ALERT = "alert"    # 告警信息
    TASK = "task"      # 操作任务
    DIALOG = "dialog"  # 对话/弹框日志


class FrontendSignal(KeySignal):
    """
    前端信号：故障现场元数据提取

    典型流程：
    1. 从 KBD/SOP 文本提取（如"检查配置存储服务备节点异常告警"）
    2. 执行 acli alert get -k "备节点异常"
    3. 解析返回的 JSON，提取 host, vm, end 等变量
    4. 写入变量池：session.variables["host"] = "node-001"

    变量生产（生产者角色）：
    - host: 故障发生节点
    - vm: 关联虚拟机
    - end: 故障发生时间
    - target: 故障目标对象
    - errcode_tracing: 错误追踪码
    """

    signal_category: SignalCategory = SignalCategory.FRONTEND
    query: FrontendQueryType = Field(
        ...,
        description="查询类型：alert（告警）/ task（任务）/ dialog（弹框）"
    )
    is_failed: bool = Field(
        default=False,
        description="是否只查失败任务（仅 query=task 时生效）"
    )
    limit: int = Field(
        default=100,
        description="最大返回记录数限制"
    )

    def extract(self) -> dict[str, Any]:
        """
        提取前端信号配置

        Returns:
            包含 query, keyword, is_failed, limit 的字典
        """
        return {
            "signal_category": self.signal_category.value,
            "query": self.query.value,
            "keyword": self.keyword,
            "is_failed": self.is_failed,
            "limit": self.limit,
            "description": self.description,
        }

    def validate(self) -> tuple[bool, str | None]:
        """
        校验前端信号参数完整性

        Returns:
            (is_valid, error_message)
        """
        if not self.keyword or not self.keyword.strip():
            return False, "关键字不能为空"

        if self.limit < 1 or self.limit > 200:
            return False, f"limit 必须在 [1, 200] 范围内，当前值: {self.limit}"

        return True, None

    async def execute(
        self,
        conversation_id: str,
        node_ip: str | None = None,
        exec_id: str | None = None,
    ) -> QKVResult:
        """
        执行前端信号提取（调用 QKV 引擎）

        Args:
            conversation_id: 会话标识
            node_ip: 执行目标节点 IP（可选）
            exec_id: 流水号追踪（可选）

        Returns:
            QKVResult: 包含提取的元数据变量列表
        """
        from app.tools.qkv.engine import qkv_exec

        return await qkv_exec(
            signal=self,
            conversation_id=conversation_id,
            node_ip=node_ip,
            exec_id=exec_id,
        )
