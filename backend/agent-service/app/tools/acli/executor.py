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
import shlex
import string
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from shared.database.redis import RedisManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from shared.utils.internal_http import InternalHTTPClient

from app.core.utils import smart_truncate
from app.tools.acli.classifier import classify_acli, classify_bash, risk_to_policy
from app.tools.acli.container_exec import BuiltCommand, ContainerCommandBuilder, ContainerExecBuildError
from app.tools.acli.semantic_validator import ToolSemanticValidator, get_allowed_bash_containers

logger = get_logger("bridge-relay-executor")


# ─────────────────────────────────────────────────────────────────────────────
# ExitCodeMeaning 和 ExecResult：执行结果数据结构与退出码定义
# ─────────────────────────────────────────────────────────────────────────────


class ExitCodeMeaning(StrEnum):
    """
    命令执行退出码语义定义
    """

    SUCCESS = "success"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    COMMAND_NOT_FOUND = "command_not_found"
    CONNECTION_REFUSED = "connection_refused"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class ExecResult:
    """
    命令执行结果

    设计依据：docs/solution/agent/agent工具设计.md §九.2

    字段说明：
      stdout: 标准输出（智能截断 ≤ 4000 chars）
      stderr: 错误输出（截断 ≤ 1000 chars）
      exit_code: 退出码（0=成功，-1=超时/净化拒绝）
      command: 实际执行的命令（净化后）
      node: 执行节点 IP
      duration_ms: 执行耗时（毫秒）
      truncated: stdout 是否被截断
      risk_level: 本次执行的风险等级（RiskClassifier 判定值）
      exit_code_meaning: 退出码的语义分类
      container: bash_exec 的结构化目标容器
      original_command: LLM 提供的容器内原始命令
      built_command: 服务端拼装后下发给 terminal_bridge 的实际命令
    """

    stdout: str
    stderr: str
    exit_code: int
    command: str
    node: str
    duration_ms: int
    truncated: bool
    risk_level: int
    exit_code_meaning: str | None = None
    container: str | None = None
    original_command: str | None = None
    built_command: str | None = None
    exec_id: str | None = None


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
      6. 换行符（[\n\r]）拦截——可绕过单条命令限制拼接出第二条命令（纵深防御）

    关于注释符 `#`：本净化器【刻意不】将 `#` 列入禁止模式。原因——本净化器作用在
    「已拼装完成的整条命令字符串」上，无法感知引号边界（quote-blind）：若某合法参数
    （如关键字 "foo#bar"）被正确 shlex.quote 包裹在单引号内，其 `#` 在 shell 中本属
    字面量、无害，但盲扫正则会误伤而拒绝一条合法命令。因此 `#`（shell 注释截断）的
    拦截责任下沉到「数据入口」——各 Handler 的 forbidden_chars（如
    GenericSubCommandHandler）在命令拼装前对原始、未引号化的用户输入做检查，那才是
    正确的信任边界。参见 handlers.py。
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
        # 换行符：可绕过单条命令限制拼接出第二条命令（纵深防御）
        (r"[\n\r]", "换行符（命令注入）"),
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

        elif tool_name == "acli_exec" and not (cleaned.startswith("acli ") or cleaned == "acli"):
            # acli_exec 必须以 acli 开头
            logger.warning(
                event="acli_exec_invalid_prefix",
                command_preview=cleaned[:50],
                message="acli_exec 命令必须以 'acli' 开头",
            )
            raise ValueError("acli_exec 命令必须以 'acli' 开头")

        return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# TemplateInterpolator：命令模板安全插值引擎
# ─────────────────────────────────────────────────────────────────────────────


class TemplateInterpolator:
    """ACLI 插件命令安全插值引擎"""

    _OPTIONAL_SEGMENT_RE = re.compile(r"\[\[([^\[\]]+)\]\]")

    @classmethod
    def interpolate(cls, template: str, args: dict[str, Any]) -> str:
        """
        根据传入参数和模板生成净化后的 Bash 命令行

        Args:
            template: 命令模板，如 "acli plugins vm_start vm_start --vm-id {vm_id}"
            args: 大模型传入的实参，如 {"vm_id": "test-123"}

        Raises:
            ValueError: 缺少必要参数或插值计算失败
        """
        if not template:
            return ""

        def render_optional_segment(match: re.Match[str]) -> str:
            segment = match.group(1)
            formatter = string.Formatter()
            placeholders = {field_name for _, field_name, _, _ in formatter.parse(segment) if field_name is not None}
            if any(args.get(placeholder) in (None, "") for placeholder in placeholders):
                return ""
            return segment

        template = cls._OPTIONAL_SEGMENT_RE.sub(render_optional_segment, template)

        # 1. 解析模板中所有的占位符
        formatter = string.Formatter()
        placeholders = {field_name for _, field_name, _, _ in formatter.parse(template) if field_name is not None}

        # 2. 检查占位符的参数是否在 args 中提供
        safe_args = {}
        for placeholder in placeholders:
            if placeholder not in args:
                raise ValueError(f"命令模板插值失败：模板中要求的参数 '{placeholder}' 在 Function Call 参数中缺失")

            val = args[placeholder]
            # 对参数值进行严格防注入处理：强转 string 并通过 shlex.quote 进行 Shell 转义
            safe_args[placeholder] = shlex.quote(str(val))

        # 3. 渲染模板
        try:
            interpolated_command = template.format(**safe_args)
        except Exception as e:
            raise ValueError(f"格式化命令模板出错: {str(e)}") from e

        return " ".join(interpolated_command.strip().split())


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
    BLPOP_TIMEOUT = 30

    def __init__(
        self,
        redis: RedisManager,
        conversation_service_url: str,
        internal_token: str,
    ) -> None:
        """
        初始化 Bridge Relay 执行器。

        Args:
            redis: Redis 管理器（用于 pending 状态 and 结果等待）
            conversation_service_url: conversation-service 内部 API 地址
            internal_token: 内部服务认证 Token
        """
        self._redis = redis
        self._conversation_service_url = conversation_service_url.rstrip("/")
        self._internal_token = internal_token
        self._http_client = InternalHTTPClient(
            base_url=self._conversation_service_url,
            timeout=32.0,  # 略大于 BLPOP_TIMEOUT，确保 HTTP 不先超时
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
        usage_template: str | None = None,
        exec_id: str | None = None,
        case_id: str = "",
        tool_def: Any | None = None,
        **kwargs,
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
            usage_template: 插件工具的命令模板（可选）
            exec_id: 统一的工具执行流水号（可选）
            case_id: 工单 ID（可选）

        Returns:
            ExecResult: 执行结果

        Raises:
            ValueError: 命令净化失败
        """
        trace_id = get_current_trace_id() or "unknown"
        start_time = time.time()
        exec_id = exec_id or str(uuid.uuid4())
        # 端到端链路：以 exec_id 作为稳定关联键（OTel trace 不存在时回退），
        # 一路透传到 conversation-service(SSE)→前端→terminal_bridge，统一分析。
        if trace_id == "unknown":
            trace_id = exec_id

        # 1. 提取命令和原因。具体工具命令必须来自 usage_template 或通用 command 参数。
        if usage_template:
            try:
                command = TemplateInterpolator.interpolate(usage_template, args)
            except ValueError as e:
                logger.error(f"插件工具插值失败: {str(e)}", exc_info=True)
                return ExecResult(
                    stdout="",
                    stderr=f"[error] 参数校验与插值失败: {str(e)}",
                    exit_code=-1,
                    command=usage_template,
                    node=node_ip or "unknown",
                    duration_ms=0,
                    truncated=False,
                    risk_level=risk_level or 3,
                )
        else:
            command = args.get("command", "") or args.get("acli", "")

        reason = args.get("reason", "未提供原因")

        semantic_result = ToolSemanticValidator.validate(tool_name, args, tool_def=tool_def)
        if not semantic_result.ok:
            feedback = semantic_result.to_feedback(tool_name)
            validation_codes = [issue.code for issue in semantic_result.issues]
            try:
                from app.services.metrics import AGENT_TOOL_SEMANTIC_VALIDATION_TOTAL

                for validation_code in validation_codes:
                    AGENT_TOOL_SEMANTIC_VALIDATION_TOTAL.labels(
                        tool_name=tool_name,
                        validation_code=validation_code,
                    ).inc()
            except Exception as met_err:
                logger.warning("metrics_record_failed", f"记录工具语义校验指标失败: {met_err}")
            logger.warning(
                event="tool_call_semantic_validation_failed",
                exec_id=exec_id,
                tool_name=tool_name,
                validation_codes=validation_codes,
                trace_id=trace_id,
            )
            return ExecResult(
                stdout="",
                stderr=feedback,
                exit_code=-1,
                command=command,
                node=node_ip or "unknown",
                duration_ms=0,
                truncated=False,
                risk_level=3,
                exit_code_meaning=ExitCodeMeaning.UNKNOWN_ERROR,
                container=str(args.get("container") or "") if tool_name == "bash_exec" else None,
                original_command=command,
            )

        original_command = command
        built: BuiltCommand | None = None

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
                container=str(args.get("container") or "") if tool_name == "bash_exec" else None,
                original_command=original_command,
            )

        if tool_name == "bash_exec":
            try:
                built = ContainerCommandBuilder.build(
                    str(args["container"]),
                    cleaned_command,
                    args.get("node_context") if isinstance(args.get("node_context"), dict) else None,
                    allowed_containers=get_allowed_bash_containers(tool_def),
                )
                cleaned_command = built.built_command
            except ContainerExecBuildError as e:
                logger.warning(
                    event="container_exec_build_failed",
                    exec_id=exec_id,
                    container=args.get("container"),
                    error=str(e),
                    trace_id=trace_id,
                )
                return ExecResult(
                    stdout="",
                    stderr=f"[container_exec] {e}",
                    exit_code=-1,
                    command=command,
                    node=node_ip or "unknown",
                    duration_ms=0,
                    truncated=False,
                    risk_level=3,
                    exit_code_meaning=ExitCodeMeaning.UNKNOWN_ERROR,
                    container=str(args.get("container") or ""),
                    original_command=original_command,
                )

        # 3. 风险分类（动态工具）
        if tool_name in ("acli_exec", "bash_exec"):
            if tool_name == "acli_exec":
                runtime_risk = classify_acli(cleaned_command)
            else:
                runtime_risk = classify_bash(original_command)
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
                container=built.container if built else None,
                original_command=built.original_command if built else original_command,
                built_command=built.built_command if built else cleaned_command,
            )

        # 5. 推送执行命令到 conversation-service
        try:
            # 诊断日志：记录即将透传给 conversation-service / terminal_bridge 的 case_id。
            # 若此处为 "(empty)"，说明调用方（acli_exec / bash_exec / qfk_exec）未携带工单 ID，
            # 将由 conversation-service 的 /agent-exec 兜底从会话解析（对齐 B1 修复）。
            logger.info(
                event="agent_exec_push",
                exec_id=exec_id,
                conversation_id=conversation_id,
                case_id=case_id or "(empty)",
                tool_name=tool_name,
                node_ip=node_ip,
                trace_id=trace_id,
            )
            resp = await self._http_client.post(
                f"/internal/conversations/{conversation_id}/agent-exec",
                json={
                    "exec_id": exec_id,
                    "tool_name": tool_name,
                    "command": cleaned_command,
                    "container": built.container if built else None,
                    "original_command": built.original_command if built else original_command,
                    "built_command": built.built_command if built else cleaned_command,
                    "reason": reason,
                    "risk_level": runtime_risk,
                    "node_ip": node_ip,
                    "case_id": case_id,  # 以 Agent 运行上下文的工单 ID 为准（不再回退 LLM 参数，避免空串透传）
                    "trace_id": trace_id,  # 端到端链路透传
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
                    container=built.container if built else None,
                    original_command=built.original_command if built else original_command,
                    built_command=built.built_command if built else cleaned_command,
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
                container=built.container if built else None,
                original_command=built.original_command if built else original_command,
                built_command=built.built_command if built else cleaned_command,
            )

        logger.info(
            event="agent_exec_pushed",
            exec_id=exec_id,
            conversation_id=conversation_id,
            command_preview=cleaned_command[:50],
            container=built.container if built else None,
            original_command_preview=(built.original_command if built else original_command)[:80],
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
                    exit_code_meaning=ExitCodeMeaning.TIMEOUT,
                    container=built.container if built else None,
                    original_command=built.original_command if built else original_command,
                    built_command=built.built_command if built else cleaned_command,
                )

            # 解析结果 JSON
            _, result_json = raw_result
            result_data = json.loads(result_json)

            exit_code = result_data.get("exit_code", 0)

            # 7. 智能截断输出并提取标准物理流 (Scheme B)
            raw_to_cache = ""
            if "stdout" in result_data or "stderr" in result_data:
                raw_stdout = result_data.get("stdout") or ""
                raw_stderr = result_data.get("stderr") or ""
                truncated = len(raw_stdout) > self.STDOUT_MAX_CHARS
                stdout = smart_truncate(raw_stdout, self.STDOUT_MAX_CHARS) if truncated else raw_stdout
                stderr = smart_truncate(raw_stderr, self.STDERR_MAX_CHARS)
                check_text = f"{raw_stdout}\n{raw_stderr}".lower()
                raw_to_cache = raw_stdout
            else:
                # 兼容原有单物理通道合并输出逻辑
                output = result_data.get("output", "")
                truncated = len(output) > self.STDOUT_MAX_CHARS
                check_text = output.lower()
                raw_to_cache = output

                # 当退出码不为0时，认为输出为错误内容，填充到 stderr；stdout 留空（或当 exit_code == 0 时反之）
                if exit_code != 0:
                    stderr = smart_truncate(output, self.STDERR_MAX_CHARS)
                    stdout = ""
                else:
                    stderr = ""
                    stdout = smart_truncate(output, self.STDOUT_MAX_CHARS) if truncated else output

            # ✅ 新增：大输出缓存到 Redis，供变量池 JIT JSONPath 提取使用
            if truncated and raw_to_cache:
                cache_key = f"cmd_cache:{exec_id}"
                try:
                    await self._redis.client.setex(cache_key, 1800, raw_to_cache.encode("utf-8"))
                    logger.info(
                        event="cmd_output_cached",
                        exec_id=exec_id,
                        cache_key=cache_key,
                        raw_size=len(raw_to_cache),
                        truncated_size=self.STDOUT_MAX_CHARS,
                    )
                except Exception as cache_err:
                    logger.warning(
                        event="cmd_output_cache_failed",
                        exec_id=exec_id,
                        error=str(cache_err),
                    )

            # 退出码语义判定
            meaning = ExitCodeMeaning.SUCCESS
            if exit_code != 0:
                meaning = ExitCodeMeaning.UNKNOWN_ERROR
                # 尝试从 stdout / stderr 识别更具体的语义
                if exit_code == 127 or "command not found" in check_text:
                    meaning = ExitCodeMeaning.COMMAND_NOT_FOUND
                    # ✅ 新增：针对 Python 命令不存在时的专项纠错引导
                    if "python" in cleaned_command.lower() and "command not found" in check_text:
                        stderr = (
                            "[Error 127] bash: python3: command not found.\n"
                            "⚠️ 自纠错提示：目标 HCI 物理宿主机【没有 Python 运行环境】。\n"
                            "请立即放弃 python 管道过滤策略，改用以下方案：\n"
                            "  - JSON 过滤：使用 jq。例：acli --formatter json storage asan disk list | jq '.data.disks[] | select(.host_name == \"目标节点名\")'\n"
                            "  - 文本过滤：使用 grep -B10 -A10。\n"
                            "重新生成命令后再次执行。"
                        )
                elif exit_code == 126 or "permission denied" in check_text:
                    meaning = ExitCodeMeaning.PERMISSION_DENIED
                elif "connection refused" in check_text:
                    meaning = ExitCodeMeaning.CONNECTION_REFUSED

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
            # 当前先返回结果，tool_result 写入由 react_engine 或专门 of AuditService 处理

            return ExecResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                command=cleaned_command,
                node=node_ip or "unknown",
                duration_ms=duration_ms,
                truncated=truncated,
                risk_level=runtime_risk,
                exit_code_meaning=meaning,
                container=built.container if built else None,
                original_command=built.original_command if built else original_command,
                built_command=built.built_command if built else cleaned_command,
                exec_id=exec_id,
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
                exit_code_meaning=ExitCodeMeaning.UNKNOWN_ERROR,
                container=built.container if built else None,
                original_command=built.original_command if built else original_command,
                built_command=built.built_command if built else cleaned_command,
                exec_id=exec_id,
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
    container: str,
    command: str,
    reason: str,
    conversation_id: str,
    node_ip: str | None = None,
) -> ExecResult:
    """
    bash_exec 工具入口函数。

    Args:
        container: 目标容器（asv-con/vn-con/vn-agent/vs-cp-manager）
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
        args={"container": container, "command": command, "reason": reason},
        conversation_id=conversation_id,
        node_ip=node_ip,
    )
