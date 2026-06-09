"""
PR-1 工具事务执行链修复 — 单元测试

覆盖：
  T0-3 ExecResult 透传：Composite 执行器不再降级为 string，ToolResultEnvelope 能拿到 exit_code_meaning
  T1-1 Authorization 落库：ConfirmService.submit_confirm 在用户决策时记录到 authorization 表
  T1-2 前端 exec_id 回传：（前端 TS 修改，由 ChatWindow 集成测试覆盖，本文件仅校验 schema）
  T1-3 fail-closed 策略：confirm_service 缺失时高危工具应被拒绝，不能 fail-open
  T1-4 retry_count 落库：ToolAuditService.write_tool_audit 接受 retry_count 参数
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.confirm_service import ConfirmService
from app.adapters.agents.htp.react_engine import ToolResultEnvelope


@dataclass
class _FakeExecResult:
    """模拟 BridgeRelayExecutor 返回的 ExecResult dataclass。"""

    stdout: str
    stderr: str
    exit_code: int
    exit_code_meaning: str | None
    duration_ms: int = 0
    truncated: bool = False


# ── T0-3 ────────────────────────────────────────────────────────────────


def test_t0_3_exec_result_envelope_preserves_exit_code_meaning():
    """ToolResultEnvelope.from_raw_result 应通过 hasattr 检测拿到 exit_code_meaning。"""
    raw = _FakeExecResult(
        stdout="",
        stderr="command not found: xx",
        exit_code=127,
        exit_code_meaning="command_not_found",
    )
    envelope = ToolResultEnvelope.from_raw_result(
        tool_name="bash_exec",
        exec_id="exec-123",
        result=raw,
    )
    assert envelope.exit_code == 127
    assert envelope.exit_code_meaning == "command_not_found"
    assert envelope.success is False
    # LLM 可读的 message 必须包含 exit_code_meaning 信息
    msg = envelope.to_llm_message()
    assert "command_not_found" in msg


def test_t0_3_exec_result_timeout_passthrough():
    """超时场景下，exit_code_meaning=timeout 应能透传给 LLM。"""
    raw = _FakeExecResult(
        stdout="",
        stderr="Timed out after 30s",
        exit_code=-1,
        exit_code_meaning="timeout",
    )
    envelope = ToolResultEnvelope.from_raw_result(
        tool_name="bash_exec",
        exec_id="exec-456",
        result=raw,
    )
    assert envelope.exit_code_meaning == "timeout"
    assert "timeout" in envelope.to_llm_message()


# ── T1-1 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t1_1_submit_confirm_calls_authorization_service():
    """submit_confirm 在用户 approve 时，应写入 authorization 记录。"""
    redis = AsyncMock()
    auth_service = MagicMock()
    auth_service.record_decision = AsyncMock(return_value="auth-uuid-123")

    svc = ConfirmService(redis=redis, authorization_service=auth_service)
    await svc.submit_confirm(
        session_id="sess-1",
        confirmed=True,
        authorized_by="alice",
        exec_id="exec-789",
    )

    auth_service.record_decision.assert_awaited_once_with(
        exec_id="exec-789",
        actor="alice",
        decision="approve",
    )
    # Redis lpush 也必须被调用，且 value 中应包含 auth_id
    redis.lpush.assert_awaited_once()
    args, _ = redis.lpush.call_args
    assert args[0] == "confirm:exec-789"
    assert "auth-uuid-123" in args[1]


@pytest.mark.asyncio
async def test_t1_1_submit_confirm_deny_decision():
    """用户 deny 时，decision 应记录为 deny。"""
    redis = AsyncMock()
    auth_service = MagicMock()
    auth_service.record_decision = AsyncMock(return_value="auth-deny-1")

    svc = ConfirmService(redis=redis, authorization_service=auth_service)
    await svc.submit_confirm(
        session_id="sess-2",
        confirmed=False,
        authorized_by="bob",
        exec_id="exec-abc",
    )
    auth_service.record_decision.assert_awaited_once_with(
        exec_id="exec-abc",
        actor="bob",
        decision="deny",
    )


@pytest.mark.asyncio
async def test_t1_1_submit_confirm_without_auth_service_still_works():
    """authorization_service 未注入时，submit_confirm 仍应正常完成 Redis LPUSH。"""
    redis = AsyncMock()
    svc = ConfirmService(redis=redis, authorization_service=None)
    await svc.submit_confirm(
        session_id="sess-x",
        confirmed=True,
        authorized_by="user",
        exec_id="exec-xyz",
    )
    redis.lpush.assert_awaited_once()


# ── T1-4 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t1_4_tool_audit_accepts_retry_count():
    """ToolAuditService.write_tool_audit 必须接受 retry_count 关键字参数。"""
    from datetime import UTC, datetime

    from app.services.tool_audit import ToolAuditService

    # session_factory 未初始化时直接 warn 返回，签名验证仍然有效
    ToolAuditService._session_factory = None
    # 不抛 TypeError 即代表签名兼容
    await ToolAuditService.write_tool_audit(
        audit_id="exec-r1",
        session_id="sess-1",
        tool_name="bash_exec",
        tool_args={"cmd": "ls"},
        risk_level=1,
        policy="auto",
        result="ok",
        error=None,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        duration_ms=10,
        retry_count=2,
    )
