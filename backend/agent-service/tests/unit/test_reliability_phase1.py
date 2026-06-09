import time

from app.services.policy_service import PolicyService
from app.services.tool_reliability import ToolCircuitBreaker, ToolRetryPolicy
from app.tools.acli.executor import ExitCodeMeaning


def test_tool_circuit_breaker():
    tool_name = "test_circuit_tool"

    # 强制清理可能残留的类变量状态
    if tool_name in ToolCircuitBreaker._states:
        del ToolCircuitBreaker._states[tool_name]

    breaker = ToolCircuitBreaker(tool_name, failure_threshold=2, recovery_timeout=0.5)

    # 初始状态
    assert breaker.allow_execution() is True

    # 第一次失败
    breaker.record_failure()
    assert breaker.allow_execution() is True

    # 第二次失败 -> 达到阈值，触发熔断
    breaker.record_failure()
    assert breaker.allow_execution() is False  # 应该熔断，不允许执行

    # 等待超过恢复超时时间
    time.sleep(0.6)
    # 应该允许执行，且状态转为 half-open
    assert breaker.allow_execution() is True
    assert breaker._state["status"] == "half-open"

    # Half-open 再次失败 -> 重新 open
    breaker.record_failure()
    assert breaker.allow_execution() is False

    # 等待再次超时
    time.sleep(0.6)
    assert breaker.allow_execution() is True

    # 成功执行 -> 应该重置状态为 closed
    breaker.record_success()
    assert breaker.allow_execution() is True
    assert breaker._state["status"] == "closed"
    assert breaker._state["fail_count"] == 0


def test_tool_retry_policy():
    # 超时应该可重试
    assert ToolRetryPolicy.is_retriable(ExitCodeMeaning.TIMEOUT, None) is True
    assert ToolRetryPolicy.is_retriable("timeout", None) is True
    assert ToolRetryPolicy.is_retriable("TIMEOUT", None) is True

    # 网络错误、超时等错误日志应该可重试
    assert ToolRetryPolicy.is_retriable(None, "Connection reset by peer") is True
    assert ToolRetryPolicy.is_retriable(None, "Operation timed out") is True
    assert ToolRetryPolicy.is_retriable(None, "Temporary failure in name resolution") is True

    # 普通错误不可重试
    assert ToolRetryPolicy.is_retriable(ExitCodeMeaning.SUCCESS, None) is False
    assert ToolRetryPolicy.is_retriable(None, "Syntax error in command") is False
    assert ToolRetryPolicy.is_retriable(None, "Permission denied") is False


def test_policy_service_evaluate():
    policy = PolicyService()

    # require_all_confirm 开启时任何时候都需要确认
    assert policy.evaluate_needs_confirm("tool", 1, require_all_confirm=True) is True

    # risk_level >= 2 时，无论什么模式都需要确认
    assert policy.evaluate_needs_confirm("tool", 2, execution_mode="aggressive") is True
    assert policy.evaluate_needs_confirm("tool", 3, execution_mode="aggressive") is True

    # risk_level <= 1 时，根据 execution_mode 判定
    assert policy.evaluate_needs_confirm("tool", 1, execution_mode="off") is True
    assert policy.evaluate_needs_confirm("tool", 1, execution_mode="safe-only") is False
    assert policy.evaluate_needs_confirm("tool", 1, execution_mode="aggressive") is False

    # direct / react 等未知默认值退避为需要确认
    assert policy.evaluate_needs_confirm("tool", 1, execution_mode="direct") is True
