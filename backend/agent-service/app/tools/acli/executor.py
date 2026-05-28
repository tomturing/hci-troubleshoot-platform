"""
Bridge Relay 执行器 — 所有 acli/bash 工具的唯一执行后端

权威来源：docs/solution/agent/agent工具设计.md §六.3

架构说明：
  - HCI 节点在客户私网，云端服务器没有直连路由
  - 所有 acli/bash 工具调用的唯一可行路径：通过 terminal_bridge.exe 中转
  - 执行流程：Agent Service → Redis → SSE → Frontend → terminal_bridge → SSH → HCI

安全模型（3 层防御）：
  层1：命令语法净化（CommandSanitizer）— 拒绝注入命令
  层2：风险分类（RiskClassifier）— 动态判定 risk=1/2/3
  层3：策略门（Policy Gate）— risk=3 直接拒绝，risk=2 需用户确认
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass

from shared.database.redis import RedisManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from shared.utils.internal_http import InternalHTTPClient

from app.tools.acli.classifier import classify_acli, classify_bash, risk_to_policy

logger = get_logger("bridge-relay-executor")


# ─────────────────────────────────────────────────────────────────────────────
# ExecResult：执行结果数据结构
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ExecResult:
    """
    命令执行结果

    设计依据：docs/solution/agent/agent工具设计.md §九.2

    字段说明：
      stdout: 标准输出（截断 ≤ 4000 chars）
      stderr: 错误输出（截断 ≤ 1000 chars）
      exit_code: 退出码（0=成功，-1=超时/净化拒绝）
      command: 实际执行的命令（净化后）
      node: 执行节点 IP
      duration_ms: 执行耗时（毫秒）
      truncated: stdout 是否被截断
      risk_level: 本次执行的风险等级（RiskClassifier 判定值）
    """

    stdout: str
    stderr: str
    exit_code: int
    command: str
    node: str
    duration_ms: int
    truncated: bool
    risk_level: int


# ─────────────────────────────────────────────────────────────────────────────
# CommandSanitizer：命令净化器
# ─────────────────────────────────────────────────────────────────────────────


class CommandSanitizer:
    """
    命令语法净化器（安全模型层1）

    设计依据：docs/solution/agent/agent工具设计.md §八.层1

    净化规则（违反时抛 ValueError）：
      1. 禁止命令替换：$(...)、`...`
      2. 禁止命令链：&&、||、;（pipe | 允许，用于输出过滤）
      3. 禁止路径穿越：../、/etc/shadow、/root/.ssh/
      4. bash_exec 禁止以 acli 开头（应使用 acli_exec）
      5. acli_exec 必须以 acli 开头
    """

    # 禁止的正则表达式模式
    _FORBIDDEN_PATTERNS = [
        # 命令替换：$(command) 或 `command`
        (r"\$\([^)]*\)", "命令替换 $(...)"),
        (r"`[^`]*`", "命令替换 `...`"),
        # 命令链：&&、||、;（但允许 pipe |）
        (r"\s*&&\s*", "命令链 &&"),
        (r"\s*\|\|\s*", "命令链 ||"),
        (r"\s*;\s*", "命令链 ;"),
        # 路径穿越：../、敏感路径
        (r"\.\./", "路径穿越 ../"),
        (r"/etc/shadow", "敏感路径 /etc/shadow"),
        (r"/root/.ssh/", "敏感路径 /root/.ssh/"),
        # 其他高危路径
        (r"/etc/passwd", "敏感路径 /etc/passwd"),
        (r"/etc/sudoers", "敏感路径 /etc/sudoers"),
    ]

    @classmethod
    def sanitize(cls, command: str, tool_name: str) -> str:
        """
        净化命令，违反规则时抛 ValueError。

        Args:
            command: 待执行的原始命令
            tool_name: 工具名称（bash_exec 或 acli_exec）

        Returns:
            净化后的命令（去除前后空格）

        Raises:
            ValueError: 命令违反净化规则
        """
        # 去除前后空格
        cleaned = command.strip()

        # 检查禁止模式
        for pattern, desc in cls._FORBIDDEN_PATTERNS:
            if re.search(pattern, cleaned):
                logger.warning(
                    event="command_sanitizer_blocked",
                    tool=tool_name,
                    command_preview=cleaned[:50],
                    reason=desc,
                )
                raise ValueError(f"命令被拒绝：包含 {desc}")

        # 工具专属规则
        if tool_name == "bash_exec":
            # bash_exec 禁止执行 acli 命令（应使用 acli_exec）
            if cleaned.startswith("acli ") or cleaned == "acli":
                logger.warning(
                    event="bash_exec_acli_blocked",
                    command_preview=cleaned[:50],
                    message="bash_exec 禁止执行 acli 命令，请使用 acli_exec",
                )
                raise ValueError("bash_exec 禁止执行 acli 命令，请使用 acli_exec 工具")

        elif tool_name == "acli_exec" and not (
            cleaned.startswith("acli ") or cleaned == "acli"
        ):
            # acli_exec 必须以 acli 开头
            logger.warning(
                event="acli_exec_invalid_prefix",
                command_preview=cleaned[:50],
                message="acli_exec 命令必须以 'acli' 开头",
            )
            raise ValueError("acli_exec 命令必须以 'acli' 开头")

        return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# BridgeRelayExecutor：Bridge 中转执行器
# ─────────────────────────────────────────────────────────────────────────────


class BridgeRelayExecutor:
    """
    Bridge 中转执行器：所有 acli/bash 工具的唯一执行后端。

    设计依据：docs/solution/agent/agent工具设计.md §七

    执行流程：
      1. 命令净化（Sanitizer）
      2. 风险分类（RiskClassifier，对 acli_exec/bash_exec）
      3. risk=3 → 直接拒绝，返回错误
      4. risk=2 → 写 Redis pending，推送 SSE confirm 卡片，等待用户确认
      5. risk=1 → 写 Redis pending，推送 SSE exec 事件
      6. 通过 Redis blpop 等待前端回传结果（超时 32s）
      7. 写入 tool_result 表（审计）

    使用方法：
        executor = BridgeRelayExecutor(redis, conversation_service_url, internal_token)
        result = await executor.execute(
            tool_name="bash_exec",
            args={"command": "df -h", "reason": "检查磁盘"},
            conversation_id="conv-123",
            node_ip="192.168.1.10",
        )
    """

    # 输出截断限制
    STDOUT_MAX_CHARS = 4000
    STDERR_MAX_CHARS = 1000

    # Redis blpop 超时（秒）
    BLPOP_TIMEOUT = 32

    def __init__(
        self,
        redis: RedisManager,
        conversation_service_url: str,
        internal_token: str,
    ) -> None:
        """
        初始化 Bridge Relay 执行器。

        Args:
            redis: Redis 管理器（用于 pending 状态和结果等待）
            conversation_service_url: conversation-service 内部 API 地址
            internal_token: 内部服务认证 Token
        """
        self._redis = redis
        self._conversation_service_url = conversation_service_url.rstrip("/")
        self._internal_token = internal_token
        self._http_client = InternalHTTPClient(
            base_url=self._conversation_service_url,
            timeout=35.0,  # 略大于 BLPOP_TIMEOUT，确保 HTTP 不先超时
        )

    async def aclose(self) -> None:
        """关闭 HTTP 客户端连接池"""
        await self._http_client.aclose()

    async def execute(
        self,
        tool_name: str,
        args: dict,
        *,
        conversation_id: str,
        node_ip: str | None = None,
        risk_level: int | None = None,
        policy: str | None = None,
    ) -> ExecResult:
        """
        执行命令并返回结果。

        Args:
            tool_name: 工具名称（bash_exec / acli_exec / 插件工具名）
            args: 工具参数（含 command、reason 等）
            conversation_id: 会话 ID（UUID）
            node_ip: 目标节点 IP（可选，从 context_variables 中读取）
            risk_level: 风险等级（可选，对插件工具使用固定值）
            policy: 执行策略（可选，对插件工具使用固定值）

        Returns:
            ExecResult: 执行结果

        Raises:
            ValueError: 命令净化失败
        """
        trace_id = get_current_trace_id()
        start_time = time.time()
        exec_id = str(uuid.uuid4())

        # 1. 提取命令和原因
        command = args.get("command", "")
        reason = args.get("reason", "未提供原因")

        # 2. 命令净化
        try:
            cleaned_command = CommandSanitizer.sanitize(command, tool_name)
        except ValueError as e:
            # 净化失败，直接返回拒绝结果
            return ExecResult(
                stdout="",
                stderr=str(e),
                exit_code=-1,
                command=command,
                node=node_ip or "unknown",
                duration_ms=0,
                truncated=False,
                risk_level=3,  # 净化拒绝视为高危
            )

        # 3. 风险分类（动态工具）
        if tool_name in ("acli_exec", "bash_exec"):
            if tool_name == "acli_exec":
                runtime_risk = classify_acli(cleaned_command)
            else:
                runtime_risk = classify_bash(cleaned_command)
            runtime_policy = risk_to_policy(runtime_risk)
        else:
            # 插件工具使用传入或默认值
            runtime_risk = risk_level or 1
            runtime_policy = policy or "auto"

        # 4. policy="block" → 直接返回拒绝
        if runtime_policy == "block":
            logger.warning(
                event="command_blocked_by_policy",
                tool=tool_name,
                command_preview=cleaned_command[:50],
                risk_level=runtime_risk,
                trace_id=trace_id,
            )
            return ExecResult(
                stdout="",
                stderr=f"[blocked] 命令 '{cleaned_command}' 属于高危操作，已拒绝执行",
                exit_code=-1,
                command=cleaned_command,
                node=node_ip or "unknown",
                duration_ms=0,
                truncated=False,
                risk_level=runtime_risk,
            )

        # 5. 推送执行命令到 conversation-service
        try:
            resp = await self._http_client.post(
                f"/internal/conversations/{conversation_id}/agent-exec",
                json={
                    "exec_id": exec_id,
                    "command": cleaned_command,
                    "reason": reason,
                    "risk_level": runtime_risk,
                    "node_ip": node_ip,
                    "case_id": args.get("case_id", ""),  # 可选，部分插件工具需要
                },
            )
            resp.raise_for_status()
            push_result = resp.json()

            if not push_result.get("ok"):
                logger.error(
                    event="agent_exec_push_failed",
                    exec_id=exec_id,
                    conversation_id=conversation_id,
                    error=push_result.get("message", "未知错误"),
                    trace_id=trace_id,
                )
                return ExecResult(
                    stdout="",
                    stderr=f"推送执行命令失败：{push_result.get('message', '未知错误')}",
                    exit_code=-1,
                    command=cleaned_command,
                    node=node_ip or "unknown",
                    duration_ms=int((time.time() - start_time) * 1000),
                    truncated=False,
                    risk_level=runtime_risk,
                )

        except Exception as e:
            logger.error(
                event="agent_exec_http_error",
                exec_id=exec_id,
                conversation_id=conversation_id,
                error=str(e),
                trace_id=trace_id,
            )
            return ExecResult(
                stdout="",
                stderr=f"HTTP 调用失败：{e}",
                exit_code=-1,
                command=cleaned_command,
                node=node_ip or "unknown",
                duration_ms=int((time.time() - start_time) * 1000),
                truncated=False,
                risk_level=runtime_risk,
            )

        logger.info(
            event="agent_exec_pushed",
            exec_id=exec_id,
            conversation_id=conversation_id,
            command_preview=cleaned_command[:50],
            risk_level=runtime_risk,
            policy=runtime_policy,
            trace_id=trace_id,
        )

        # 6. 等待前端回传结果（Redis blpop）
        result_key = f"exec_result:{exec_id}"
        try:
            # blpop 返回 (key, value) 元组，超时返回 None
            raw_result = await self._redis.client.blpop(result_key, timeout=self.BLPOP_TIMEOUT)

            if raw_result is None:
                # 超时
                logger.warning(
                    event="exec_result_timeout",
                    exec_id=exec_id,
                    conversation_id=conversation_id,
                    timeout_sec=self.BLPOP_TIMEOUT,
                    trace_id=trace_id,
                )
                return ExecResult(
                    stdout="",
                    stderr=f"执行超时（{self.BLPOP_TIMEOUT}秒），可能前端未响应或 terminal_bridge 未运行",
                    exit_code=-1,
                    command=cleaned_command,
                    node=node_ip or "unknown",
                    duration_ms=int((time.time() - start_time) * 1000),
                    truncated=False,
                    risk_level=runtime_risk,
                )

            # 解析结果 JSON
            _, result_json = raw_result
            result_data = json.loads(result_json)

            output = result_data.get("output", "")
            exit_code = result_data.get("exit_code", 0)

            # 7. 截断输出
            truncated = len(output) > self.STDOUT_MAX_CHARS
            stdout = output[:self.STDOUT_MAX_CHARS] if truncated else output
            stderr = ""

            duration_ms = int((time.time() - start_time) * 1000)

            logger.info(
                event="exec_result_received",
                exec_id=exec_id,
                conversation_id=conversation_id,
                exit_code=exit_code,
                output_preview=stdout[:100],
                truncated=truncated,
                duration_ms=duration_ms,
                trace_id=trace_id,
            )

            # 8. 写入 tool_result 表（审计）— TODO: 后续实现
            # 当前先返回结果，tool_result 写入由 react_engine 或专门的 AuditService 处理

            return ExecResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                command=cleaned_command,
                node=node_ip or "unknown",
                duration_ms=duration_ms,
                truncated=truncated,
                risk_level=runtime_risk,
            )

        except Exception as e:
            logger.error(
                event="exec_result_parse_error",
                exec_id=exec_id,
                conversation_id=conversation_id,
                error=str(e),
                trace_id=trace_id,
            )
            return ExecResult(
                stdout="",
                stderr=f"结果解析失败：{e}",
                exit_code=-1,
                command=cleaned_command,
                node=node_ip or "unknown",
                duration_ms=int((time.time() - start_time) * 1000),
                truncated=False,
                risk_level=runtime_risk,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数入口（供 react_engine 直接调用）
# ─────────────────────────────────────────────────────────────────────────────


# 全局执行器实例（由 main.py lifespan 初始化）
_executor: BridgeRelayExecutor | None = None


def set_executor(executor: BridgeRelayExecutor) -> None:
    """设置全局执行器实例（main.py lifespan 调用）"""
    global _executor
    _executor = executor


async def acli_exec(
    command: str,
    reason: str,
    conversation_id: str,
    node_ip: str | None = None,
) -> ExecResult:
    """
    acli_exec 工具入口函数。

    Args:
        command: acli 命令（必须以 'acli' 开头）
        reason: 执行原因（审计必填）
        conversation_id: 会话 ID
        node_ip: 目标节点 IP（可选）

    Returns:
        ExecResult: 执行结果
    """
    if _executor is None:
        raise RuntimeError("BridgeRelayExecutor 未初始化")

    return await _executor.execute(
        tool_name="acli_exec",
        args={"command": command, "reason": reason},
        conversation_id=conversation_id,
        node_ip=node_ip,
    )


async def bash_exec(
    command: str,
    reason: str,
    conversation_id: str,
    node_ip: str | None = None,
) -> ExecResult:
    """
    bash_exec 工具入口函数。

    Args:
        command: bash 命令
        reason: 执行原因（审计必填）
        conversation_id: 会话 ID
        node_ip: 目标节点 IP（可选）

    Returns:
        ExecResult: 执行结果
    """
    if _executor is None:
        raise RuntimeError("BridgeRelayExecutor 未初始化")

    return await _executor.execute(
        tool_name="bash_exec",
        args={"command": command, "reason": reason},
        conversation_id=conversation_id,
        node_ip=node_ip,
    )
