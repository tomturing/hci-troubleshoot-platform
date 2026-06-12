"""
BridgeRelayExecutor 单元测试

验收标准（docs/task/agent/agent工具任务清单.md T-TOOL-12）：
  1. CommandSanitizer 拒绝注入命令（$(ls)、; rm -rf）
  2. blpop 超时返回 exit_code=-1
  3. tool_result 表有记录（TODO：后续集成测试验证）
  4. stdout 超 4000 chars 被截断
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.tools.acli.executor import (
    BridgeRelayExecutor,
    CommandSanitizer,
    ExecResult,
    ExitCodeMeaning,
)

# ─────────────────────────────────────────────────────────────────────────────
# CommandSanitizer 测试（验收标准 1）
# ─────────────────────────────────────────────────────────────────────────────


class TestCommandSanitizer:
    """命令净化器单元测试"""

    # ── 测试验收标准 1：拒绝注入命令 ──────────────────────────────────────

    def test_reject_command_substitution_dollar(self):
        """拒绝 $(command) 命令替换"""
        with pytest.raises(ValueError, match="命令替换"):
            CommandSanitizer.sanitize("echo $(ls)", "bash_exec")

    def test_reject_command_substitution_backtick(self):
        """拒绝 `command` 命令替换"""
        with pytest.raises(ValueError, match="命令替换"):
            CommandSanitizer.sanitize("echo `whoami`", "bash_exec")

    def test_reject_command_chain_and(self):
        """拒绝 && 命令链"""
        with pytest.raises(ValueError, match="命令链"):
            CommandSanitizer.sanitize("ls && rm -rf /", "bash_exec")

    def test_reject_command_chain_or(self):
        """拒绝 || 命令链"""
        with pytest.raises(ValueError, match="命令链"):
            CommandSanitizer.sanitize("ls || echo failed", "bash_exec")

    def test_reject_command_chain_semicolon(self):
        """拒绝 ; 命令链"""
        with pytest.raises(ValueError, match="命令链"):
            CommandSanitizer.sanitize("ls; rm -rf /", "bash_exec")

    def test_reject_path_traversal(self):
        """拒绝 ../ 路径穿越"""
        with pytest.raises(ValueError, match="路径穿越"):
            CommandSanitizer.sanitize("cat ../../../etc/passwd", "bash_exec")

    def test_reject_sensitive_path_shadow(self):
        """拒绝 /etc/shadow 敏感路径"""
        with pytest.raises(ValueError, match="敏感路径"):
            CommandSanitizer.sanitize("cat /etc/shadow", "bash_exec")

    def test_reject_sensitive_path_ssh(self):
        """拒绝 /root/.ssh/ 敏感路径"""
        with pytest.raises(ValueError, match="敏感路径"):
            CommandSanitizer.sanitize("cat /root/.ssh/id_rsa", "bash_exec")

    def test_reject_sensitive_path_passwd(self):
        """拒绝 /etc/passwd 敏感路径"""
        with pytest.raises(ValueError, match="敏感路径"):
            CommandSanitizer.sanitize("cat /etc/passwd", "bash_exec")

    def test_reject_sensitive_path_sudoers(self):
        """拒绝 /etc/sudoers 敏感路径"""
        with pytest.raises(ValueError, match="敏感路径"):
            CommandSanitizer.sanitize("cat /etc/sudoers", "bash_exec")

    # ── 测试验收标准 1（补充）：bash_exec 禁止 acli 命令 ─────────────────────

    def test_bash_exec_reject_acli_prefix(self):
        """bash_exec 禁止执行 acli 命令"""
        with pytest.raises(ValueError, match="bash_exec 禁止执行 acli 命令"):
            CommandSanitizer.sanitize("acli vm list", "bash_exec")

    def test_bash_exec_reject_acli_only(self):
        """bash_exec 禁止仅 acli 命令"""
        with pytest.raises(ValueError, match="bash_exec 禁止执行 acli 命令"):
            CommandSanitizer.sanitize("acli", "bash_exec")

    # ── 测试验收标准 1（补充）：acli_exec 必须以 acli 开头 ───────────────────

    def test_acli_exec_require_prefix(self):
        """acli_exec 命令必须以 acli 开头"""
        with pytest.raises(ValueError, match="必须以 'acli' 开头"):
            CommandSanitizer.sanitize("ls -la", "acli_exec")

    def test_acli_exec_accept_valid_prefix(self):
        """acli_exec 接受合法命令"""
        result = CommandSanitizer.sanitize("acli --formatter json vm list", "acli_exec")
        assert result == "acli --formatter json vm list"

    # ── 测试允许的合法命令 ───────────────────────────────────────────────────

    def test_allow_pipe_operator(self):
        """允许 pipe | 操作符（输出过滤）"""
        result = CommandSanitizer.sanitize("df -h | grep sda", "bash_exec")
        assert result == "df -h | grep sda"

    def test_allow_normal_bash_command(self):
        """允许普通 bash 命令"""
        result = CommandSanitizer.sanitize("df -h", "bash_exec")
        assert result == "df -h"

    def test_allow_normal_acli_command(self):
        """允许普通 acli 命令"""
        result = CommandSanitizer.sanitize("acli vm list", "acli_exec")
        assert result == "acli vm list"

    def test_strip_whitespace(self):
        """去除前后空格"""
        result = CommandSanitizer.sanitize("  df -h  ", "bash_exec")
        assert result == "df -h"


# ─────────────────────────────────────────────────────────────────────────────
# BridgeRelayExecutor 测试（验收标准 2 & 4）
# ─────────────────────────────────────────────────────────────────────────────


class TestBridgeRelayExecutor:
    """Bridge Relay 执行器单元测试"""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis 管理器"""
        redis_manager = MagicMock()
        redis_manager.client = AsyncMock()
        return redis_manager

    @pytest.fixture
    def executor(self, mock_redis):
        """创建测试执行器实例"""
        return BridgeRelayExecutor(
            redis=mock_redis,
            conversation_service_url="http://conversation-service:8002",
            internal_token="test-token",
        )

    # ── 测试验收标准 2：blpop 超时返回 exit_code=-1 ───────────────────────────

    @pytest.mark.asyncio
    async def test_blpop_timeout_returns_exit_code_minus_one(self, executor, mock_redis):
        """blpop 超时返回 exit_code=-1"""
        # Mock HTTP 调用成功
        with patch.object(executor._http_client, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"ok": True, "exec_id": "test-exec-id"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            # Mock blpop 超时返回 None
            mock_redis.client.blpop.return_value = None

            result = await executor.execute(
                tool_name="bash_exec",
                args={"container": "asv-con", "command": "df -h", "reason": "测试超时"},
                conversation_id="conv-123",
            )

            assert result.exit_code == -1
            assert "超时" in result.stderr
            assert result.risk_level == 1  # df -h 是只读命令

    # ── 测试验收标准 4：stdout 超 4000 chars 被截断 ───────────────────────────

    @pytest.mark.asyncio
    async def test_stdout_truncated_over_4000_chars(self, executor, mock_redis):
        """stdout 超 4000 chars 被截断"""
        # Mock HTTP 调用成功
        with patch.object(executor._http_client, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"ok": True, "exec_id": "test-exec-id"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            # Mock blpop 返回超长输出
            long_output = "A" * 5000
            mock_redis.client.blpop.return_value = (
                "exec_result:test-exec-id",
                json.dumps({"output": long_output, "exit_code": 0}),
            )

            result = await executor.execute(
                tool_name="bash_exec",
                args={"container": "asv-con", "command": "cat /var/log/big.log", "reason": "测试截断"},
                conversation_id="conv-123",
            )

            assert result.truncated is True
            assert len(result.stdout) <= 4000
            assert "此处截断" in result.stdout
            assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_stdout_not_truncated_under_4000_chars(self, executor, mock_redis):
        """stdout 不超 4000 chars 不截断"""
        # Mock HTTP 调用成功
        with patch.object(executor._http_client, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"ok": True, "exec_id": "test-exec-id"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            # Mock blpop 返回正常输出
            normal_output = "A" * 3000
            mock_redis.client.blpop.return_value = (
                "exec_result:test-exec-id",
                json.dumps({"output": normal_output, "exit_code": 0}),
            )

            result = await executor.execute(
                tool_name="bash_exec",
                args={"container": "asv-con", "command": "df -h", "reason": "测试"},
                conversation_id="conv-123",
            )

            assert not result.truncated
            assert len(result.stdout) == 3000
            assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_dual_channel_stderr_preserved_on_nonzero_exit(self, executor, mock_redis):
        """双通道失败时保留真实 stderr，不再被 output 未定义异常覆盖"""
        with patch.object(executor._http_client, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"ok": True, "exec_id": "test-exec-id"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            mock_redis.client.blpop.return_value = (
                "exec_result:test-exec-id",
                json.dumps({"stdout": "", "stderr": "invalid option", "exit_code": 2}),
            )

            result = await executor.execute(
                tool_name="bash_exec",
                args={"container": "asv-con", "command": "ps --bad-option", "reason": "测试 stderr"},
                conversation_id="conv-123",
            )

            assert result.exit_code == 2
            assert result.stdout == ""
            assert "invalid option" in result.stderr
            assert "cannot access local variable 'output'" not in result.stderr
            assert result.exit_code_meaning == ExitCodeMeaning.UNKNOWN_ERROR

    # ── 测试命令净化失败返回拒绝 ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_sanitizer_rejection_returns_blocked_result(self, executor, mock_redis):
        """命令净化失败返回拒绝结果"""
        result = await executor.execute(
            tool_name="bash_exec",
            args={"container": "asv-con", "command": "ls && rm -rf", "reason": "测试净化拒绝"},
            conversation_id="conv-123",
        )

        # 不调用 HTTP 和 Redis，直接返回拒绝
        assert result.exit_code == -1
        assert "命令链" in result.stderr or "拒绝" in result.stderr
        assert result.risk_level == 3  # 净化拒绝视为高危

    # ── 测试 policy=block 直接拒绝 ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_block_policy_direct_rejection(self, executor, mock_redis):
        """policy=block 直接拒绝"""
        # acli vm delete 被 classifier 识别为 risk=3 (block)
        result = await executor.execute(
            tool_name="acli_exec",
            args={"command": "acli vm delete abc-123", "reason": "测试 block"},
            conversation_id="conv-123",
        )

        # 不调用 HTTP 和 Redis，直接返回拒绝
        assert result.exit_code == -1
        assert "blocked" in result.stderr.lower() or "拒绝" in result.stderr
        assert result.risk_level == 3

    # ── 测试风险分类动态判定 ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_risk_classification_acli_readonly(self, executor, mock_redis):
        """acli 只读命令 risk=1"""
        # Mock HTTP 调用成功
        with patch.object(executor._http_client, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"ok": True, "exec_id": "test-exec-id"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            # Mock blpop 返回结果
            mock_redis.client.blpop.return_value = (
                "exec_result:test-exec-id",
                json.dumps({"output": "success", "exit_code": 0}),
            )

            result = await executor.execute(
                tool_name="acli_exec",
                args={"command": "acli --formatter json vm list", "reason": "测试"},
                conversation_id="conv-123",
            )

            assert result.risk_level == 1  # list 是只读操作

    @pytest.mark.asyncio
    async def test_risk_classification_acli_write(self, executor, mock_redis):
        """acli 写命令 risk=2"""
        # Mock HTTP 调用成功
        with patch.object(executor._http_client, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"ok": True, "exec_id": "test-exec-id"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            # Mock blpop 返回结果
            mock_redis.client.blpop.return_value = (
                "exec_result:test-exec-id",
                json.dumps({"output": "success", "exit_code": 0}),
            )

            result = await executor.execute(
                tool_name="acli_exec",
                args={"command": "acli service asv vtpdaemon restart", "reason": "测试"},
                conversation_id="conv-123",
            )

            assert result.risk_level == 2  # restart 是写操作

    # ── 测试 HTTP 调用失败 ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_http_call_failure(self, executor, mock_redis):
        """HTTP 调用失败返回错误"""
        with patch.object(executor._http_client, "post") as mock_post:
            mock_post.side_effect = Exception("Connection refused")

            result = await executor.execute(
                tool_name="bash_exec",
                args={"container": "asv-con", "command": "df -h", "reason": "测试 HTTP 失败"},
                conversation_id="conv-123",
            )

            assert result.exit_code == -1
            assert "Connection refused" in result.stderr

    # ── 测试结果解析失败 ───────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_result_parse_error(self, executor, mock_redis):
        """结果 JSON 解析失败"""
        with patch.object(executor._http_client, "post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"ok": True, "exec_id": "test-exec-id"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value = mock_response

            # Mock blpop 返回无效 JSON
            mock_redis.client.blpop.return_value = (
                "exec_result:test-exec-id",
                "invalid-json",
            )

            result = await executor.execute(
                tool_name="bash_exec",
                args={"container": "asv-con", "command": "df -h", "reason": "测试解析失败"},
                conversation_id="conv-123",
            )

            assert result.exit_code == -1
            assert "解析失败" in result.stderr


# ─────────────────────────────────────────────────────────────────────────────
# ExecResult 测试
# ─────────────────────────────────────────────────────────────────────────────


class TestExecResult:
    """ExecResult 数据结构测试"""

    def test_exec_result_creation(self):
        """测试 ExecResult 创建"""
        result = ExecResult(
            stdout="output",
            stderr="",
            exit_code=0,
            command="df -h",
            node="192.168.1.10",
            duration_ms=150,
            truncated=False,
            risk_level=1,
        )

        assert result.stdout == "output"
        assert result.exit_code == 0
        assert result.command == "df -h"
        assert result.node == "192.168.1.10"
        assert result.duration_ms == 150
        assert not result.truncated
        assert result.risk_level == 1

    def test_exec_result_timeout_indicator(self):
        """测试 ExecResult 超时标识（exit_code=-1）"""
        result = ExecResult(
            stdout="",
            stderr="执行超时",
            exit_code=-1,
            command="df -h",
            node="unknown",
            duration_ms=32000,
            truncated=False,
            risk_level=1,
        )

        assert result.exit_code == -1  # 超时标识
        assert "超时" in result.stderr

    def test_exec_result_truncated_indicator(self):
        """测试 ExecResult 截断标识"""
        result = ExecResult(
            stdout="A" * 4000,
            stderr="",
            exit_code=0,
            command="cat big.log",
            node="192.168.1.10",
            duration_ms=100,
            truncated=True,
            risk_level=1,
        )

        assert result.truncated is True
        assert len(result.stdout) == 4000


# ─────────────────────────────────────────────────────────────────────────────
# 工具入口函数测试
# ─────────────────────────────────────────────────────────────────────────────


class TestToolEntryFunctions:
    """工具入口函数测试"""

    def test_acli_exec_requires_executor_initialized(self):
        """acli_exec 需执行器已初始化"""
        from app.tools.acli.executor import acli_exec

        # 未初始化时抛 RuntimeError
        with pytest.raises(RuntimeError, match="未初始化"):
            import asyncio

            asyncio.run(acli_exec("acli vm list", "测试", "conv-123"))

    def test_bash_exec_requires_executor_initialized(self):
        """bash_exec 需执行器已初始化"""
        from app.tools.acli.executor import bash_exec

        # 未初始化时抛 RuntimeError
        with pytest.raises(RuntimeError, match="未初始化"):
            import asyncio

            asyncio.run(bash_exec("asv-con", "df -h", "测试", "conv-123"))

    def test_set_executor(self):
        """测试 set_executor 函数"""
        from app.tools.acli.executor import set_executor

        mock_redis = MagicMock()
        mock_executor = BridgeRelayExecutor(
            redis=mock_redis,
            conversation_service_url="http://test",
            internal_token="token",
        )

        set_executor(mock_executor)

        # 验证全局执行器已设置
        import app.tools.acli.executor as executor_module

        assert executor_module._executor is mock_executor
