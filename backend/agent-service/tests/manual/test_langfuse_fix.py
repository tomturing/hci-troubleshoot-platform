#!/usr/bin/env python3
"""
Langfuse v3 SDK 集成修复验证脚本

此脚本用于验证 Langfuse observation 数据是否正确写入。

使用方法：
    cd /aihci/hci-troubleshoot-platform
    uv run python backend/agent-service/tests/manual/test_langfuse_fix.py
"""

import os
import sys

# 添加项目路径
backend_path = os.path.join(os.path.dirname(__file__), '..', '..', '..')
sys.path.insert(0, backend_path)

# 设置 PYTHONPATH 环境变量
os.environ['PYTHONPATH'] = backend_path

from app.tools.acli.executor import ExecResult, exec_result_observation


def test_exec_result_observation():
    """测试 exec_result_observation 函数的数据完整性"""
    print("=" * 60)
    print("测试 exec_result_observation 函数")
    print("=" * 60)

    # 创建测试数据 - 成功案例
    success_result = ExecResult(
        stdout="成功执行的输出",
        stderr="",
        exit_code=0,
        command="test command",
        node="test-node",
        duration_ms=100,
        truncated=False,
        risk_level=1,
        exec_id="exec-success-123",
        trace_id="abc123",
        artifact_id="artifact-123",
        stdout_sha256="sha256-abc",
        stderr_sha256=None,
        stdout_bytes=100,
        stderr_bytes=0,
    )

    obs_data = exec_result_observation(success_result)

    print("\n成功案例:")
    print(f"  success: {obs_data.get('success')}")
    print(f"  exit_code: {obs_data.get('exit_code')}")
    print(f"  exit_code_meaning: {obs_data.get('exit_code_meaning')}")
    print(f"  stdout_preview: {obs_data.get('stdout_preview')}")
    print(f"  error_summary: {obs_data.get('error_summary')}")

    # 验证必要字段
    assert "exec_id" in obs_data, "缺少 exec_id 字段"
    assert "exit_code" in obs_data, "缺少 exit_code 字段"
    assert "success" in obs_data, "缺少 success 字段"
    assert "stdout_preview" in obs_data, "缺少 stdout_preview 字段"
    assert obs_data["success"] is True, "成功案例 success 应为 True"
    assert obs_data["error_summary"] is None, "成功案例 error_summary 应为 None"

    # 创建测试数据 - 失败案例
    failure_result = ExecResult(
        stdout="",
        stderr="Error: command failed",
        exit_code=1,
        command="test command",
        node="test-node",
        duration_ms=100,
        truncated=False,
        risk_level=1,
        exec_id="exec-failure-456",
        trace_id="def456",
        artifact_id="artifact-456",
        stdout_sha256=None,
        stderr_sha256="sha256-def",
        stdout_bytes=0,
        stderr_bytes=50,
        error_type="command_failed",
    )

    obs_data = exec_result_observation(failure_result)

    print("\n失败案例:")
    print(f"  success: {obs_data.get('success')}")
    print(f"  exit_code: {obs_data.get('exit_code')}")
    print(f"  exit_code_meaning: {obs_data.get('exit_code_meaning')}")
    print(f"  stderr_preview: {obs_data.get('stderr_preview')}")
    print(f"  error_summary: {obs_data.get('error_summary')}")

    # 验证必要字段
    assert "exec_id" in obs_data, "缺少 exec_id 字段"
    assert "exit_code" in obs_data, "缺少 exit_code 字段"
    assert "success" in obs_data, "缺少 success 字段"
    assert "stderr_preview" in obs_data, "缺少 stderr_preview 字段"
    assert obs_data["success"] is False, "失败案例 success 应为 False"
    assert obs_data["error_summary"] is not None, "失败案例 error_summary 不应为 None"

    # 创建测试数据 - 超时案例
    timeout_result = ExecResult(
        stdout="",
        stderr="Command timed out",
        exit_code=124,
        command="test command",
        node="test-node",
        duration_ms=10000,
        truncated=False,
        risk_level=1,
        exec_id="exec-timeout-789",
        trace_id="ghi789",
        artifact_id="artifact-789",
        stdout_sha256=None,
        stderr_sha256="sha256-ghi",
        stdout_bytes=0,
        stderr_bytes=100,
        error_type="timeout",
    )

    obs_data = exec_result_observation(timeout_result)

    print("\n超时案例:")
    print(f"  success: {obs_data.get('success')}")
    print(f"  exit_code: {obs_data.get('exit_code')}")
    print(f"  exit_code_meaning: {obs_data.get('exit_code_meaning')}")
    print(f"  error_summary: {obs_data.get('error_summary')}")

    # 验证必要字段
    assert obs_data["exit_code_meaning"] == "timeout", "超时案例 exit_code_meaning 应为 'timeout'"
    assert "timeout" in obs_data["error_summary"].lower(), "超时案例 error_summary 应包含 'timeout'"

    print("\n" + "=" * 60)
    print("✅ 所有测试通过！")
    print("=" * 60)


if __name__ == "__main__":
    test_exec_result_observation()
