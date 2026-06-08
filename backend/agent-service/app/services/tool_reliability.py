import logging
import time
from typing import Any

from app.tools.acli.executor import ExitCodeMeaning

logger = logging.getLogger("agent.reliability")

class ToolCircuitBreaker:
    """单个工具的内存熔断器"""

    # 内存存储结构: { tool_name: { "status": "closed/open/half-open", "fail_count": 0, "last_state_change": timestamp } }
    _states: dict[str, dict[str, Any]] = {}

    def __init__(
        self,
        tool_name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
    ):
        self.tool_name = tool_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        if tool_name not in ToolCircuitBreaker._states:
            ToolCircuitBreaker._states[tool_name] = {
                "status": "closed",
                "fail_count": 0,
                "last_state_change": time.time()
            }

    @property
    def _state(self) -> dict[str, Any]:
        return ToolCircuitBreaker._states[self.tool_name]

    def allow_execution(self) -> bool:
        """检查当前是否允许执行"""
        now = time.time()
        state = self._state

        if state["status"] == "closed":
            return True

        if state["status"] == "open":
            # 冷却期过，切换为 half-open 探测状态
            if now - state["last_state_change"] > self.recovery_timeout:
                logger.info(f"工具 {self.tool_name} 熔断冷却期满，进入 Half-Open 探测状态")
                state["status"] = "half-open"
                state["last_state_change"] = now
                return True
            return False

        if state["status"] == "half-open":
            return True

        return True

    def record_success(self):
        """记录成功调用并重置熔断"""
        state = self._state
        if state["status"] != "closed":
            logger.info(f"工具 {self.tool_name} 执行成功，熔断器重置为 Closed")
        state["status"] = "closed"
        state["fail_count"] = 0
        state["last_state_change"] = time.time()

    def record_failure(self):
        """记录失败调用"""
        state = self._state
        state["fail_count"] += 1
        state["last_state_change"] = time.time()

        if state["status"] == "half-open":
            logger.warning(f"工具 {self.tool_name} 在 Half-Open 状态下再次失败，重新进入 Open 熔断状态")
            state["status"] = "open"
        elif state["status"] == "closed" and state["fail_count"] >= self.failure_threshold:
            logger.warning(f"工具 {self.tool_name} 连续失败 {state['fail_count']} 次，熔断器进入 Open 状态，开始 60s 隔离")
            state["status"] = "open"


class ToolRetryPolicy:
    """工具执行重试策略判断"""

    @staticmethod
    def is_retriable(exit_code_meaning: Any, error_msg: str | None) -> bool:
        """判定是否属于可重试的网络/超时临时故障"""
        # 1. 检查 exit_code_meaning 标识
        if exit_code_meaning == ExitCodeMeaning.TIMEOUT or str(exit_code_meaning).lower() == "timeout":
            return True

        # 2. 检查报错文本中的网络超时/连接丢失等关键字
        if error_msg:
            err_lower = error_msg.lower()
            retriable_keywords = [
                "timeout",
                "timed out",
                "connection reset",
                "broken pipe",
                "temporary failure",
                "network unreachable",
                "connection refused"
            ]
            if any(kw in err_lower for kw in retriable_keywords):
                return True

        return False
