"""
ReactEngine: ReAct 循环执行引擎

职责：
  - 执行工具调用循环（Reason → Act → Observe）
  - 高危操作确认（ConfirmService）
  - 工具结果处理
  - 流式输出推理过程

被 InvestigationAgent 内部使用（execution_mode=react 时）
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from opentelemetry import trace
from pydantic import BaseModel
from shared.clients import AIAssistantRegistry
from shared.observability.langfuse import observe_tool, start_agent_observation
from shared.observability.logger import get_logger

from app.config import settings
from app.domain.agent_port import (
    AgentEvent,
    AgentInteractiveRequest,
    AgentStageUpdate,
    AgentTextChunk,
    ToolResultEvent,
)

# T-AGT-25: 导入 VariableRequestResult
from app.memory.variable_pool import VariableRequestResult

logger = get_logger("react-engine")
tracer = trace.get_tracer(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ToolCallValidator 和 ToolResultEnvelope 数据结构与校验器定义
# ─────────────────────────────────────────────────────────────────────────────


class ToolCallValidator:
    """
    轻量级无依赖的 JSON Schema 与参数有效性校验器（T0-5 扩展 enum/array/oneOf）
    """

    @staticmethod
    def validate(tool_name: str, args: dict[str, Any], parameters_schema: dict[str, Any]) -> tuple[bool, str | None]:
        if not parameters_schema:
            return True, None

        required = parameters_schema.get("required", [])
        properties = parameters_schema.get("properties", {})

        # 1. 必填参数校验
        for req_field in required:
            if req_field not in args:
                return False, f"缺少必填参数: '{req_field}'"

        # 2. 类型及格式校验
        for name, value in args.items():
            if name not in properties:
                # 允许传入额外的不在 schema 中的参数而不报错
                continue

            prop_def = properties[name]
            expected_type = prop_def.get("type")

            # T0-5: enum 校验（优先于 type 校验）
            enum_values = prop_def.get("enum")
            if enum_values is not None:
                if value not in enum_values:
                    return False, f"参数 '{name}' 值 '{value}' 不在允许的枚举值中: {enum_values}"
                continue  # enum 校验通过后跳过后续类型检查

            # 类型校验
            if expected_type == "string":
                if not isinstance(value, str):
                    return False, f"参数 '{name}' 类型错误: 期望 string，实际为 {type(value).__name__}"

                # 正则 pattern 校验
                pattern = prop_def.get("pattern")
                if pattern:
                    try:
                        if not re.match(pattern, value):
                            return False, f"参数 '{name}' 格式不正确: 必须符合正则 '{pattern}'"
                    except Exception:
                        pass

                # IP 字段/格式的强制 IPv4/主机名校验
                if prop_def.get("format") == "ipv4":
                    ip_regex = r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
                    if not re.match(ip_regex, value):
                        return False, f"参数 '{name}' 格式错误: '{value}' 不是有效的 IPv4 地址"
                elif "ip" in name.lower():
                    ip_regex = r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
                    hostname_regex = r"^[a-zA-Z0-9_.-]+$"
                    if not (re.match(ip_regex, value) or re.match(hostname_regex, value)):
                        return False, f"参数 '{name}' 格式错误: '{value}' 不是有效的 IPv4 地址或主机名"

            elif expected_type == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    return False, f"参数 '{name}' 类型错误: 期望 integer，实际为 {type(value).__name__}"

            elif expected_type == "number":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False, f"参数 '{name}' 类型错误: 期望 number，实际为 {type(value).__name__}"

            elif expected_type == "boolean":
                if not isinstance(value, bool):
                    return False, f"参数 '{name}' 类型错误: 期望 boolean，实际为 {type(value).__name__}"

            # T0-5: array 校验
            elif expected_type == "array":
                if not isinstance(value, list):
                    return False, f"参数 '{name}' 类型错误: 期望 array，实际为 {type(value).__name__}"
                # items 校验（仅支持简单类型的 items）
                items_def = prop_def.get("items")
                if items_def and isinstance(items_def, dict):
                    item_type = items_def.get("type")
                    item_enum = items_def.get("enum")
                    for i, item in enumerate(value):
                        if item_enum and item not in item_enum:
                            return False, f"参数 '{name}' 第 {i + 1} 个元素 '{item}' 不在允许枚举值中: {item_enum}"
                        elif item_type == "string" and not isinstance(item, str):
                            return False, f"参数 '{name}' 第 {i + 1} 个元素类型错误: 期望 string"
                        elif item_type == "integer" and (not isinstance(item, int) or isinstance(item, bool)):
                            return False, f"参数 '{name}' 第 {i + 1} 个元素类型错误: 期望 integer"

        # T0-5: oneOf 校验（仅支持简单 oneOf，值必须匹配其中一种 schema）
        # oneOf 通常在根 schema 或单个属性定义中出现
        root_one_of = parameters_schema.get("oneOf")
        if root_one_of:
            matched_any = False
            for schema_option in root_one_of:
                # 递归校验（简化版：仅校验 required 匹配）
                option_required = schema_option.get("required", [])
                # 检查是否满足此 schema 的 required
                if all(r in args for r in option_required):
                    # 检查属性类型是否匹配
                    inner_valid, _ = ToolCallValidator.validate(tool_name, args, schema_option)
                    if inner_valid:
                        matched_any = True
                        break
            if not matched_any:
                return False, "参数组合不满足 any oneOf schema 定义"

        return True, None


@dataclass
class ToolResultEnvelope:
    tool_name: str
    exec_id: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    exit_code_meaning: str | None = None
    truncated: bool = False
    interpretation: str | None = None
    suggested_next_action: str | None = None

    def to_llm_message(self) -> str:
        status_emoji = "✅" if self.success else "❌"
        status_text = "SUCCESS" if self.success else "FAILED"

        parts = [
            f"🛠️ [Tool: {self.tool_name}] (Execution ID: {self.exec_id})",
            f"📊 Status: {status_emoji} {status_text} | Exit Code: {self.exit_code}",
        ]

        if self.exit_code_meaning:
            parts.append(f"🔍 Exit Code Meaning: {self.exit_code_meaning}")

        if self.stdout:
            parts.append(f"📝 [Stdout]:\n{self.stdout.strip()}")

        if self.stderr:
            parts.append(f"⚠️ [Stderr]:\n{self.stderr.strip()}")

        if self.truncated:
            parts.append("⚠️ Note: Output has been truncated to avoid token overload.")

        if self.interpretation:
            parts.append(f"💡 Interpretation: {self.interpretation}")

        if self.suggested_next_action:
            parts.append(f"👉 Suggested Next Action: {self.suggested_next_action}")

        return "\n".join(parts)

    @classmethod
    def from_raw_result(
        cls,
        tool_name: str,
        exec_id: str,
        result: Any,
        error: str | None = None,
    ) -> ToolResultEnvelope:
        if isinstance(result, cls):
            return result

        # 检查是否为 ExecResult
        if hasattr(result, "exit_code") and hasattr(result, "stdout") and hasattr(result, "stderr"):
            exit_code = result.exit_code
            stdout = result.stdout
            stderr = result.stderr
            truncated = getattr(result, "truncated", False)
            exit_code_meaning = getattr(result, "exit_code_meaning", None)

            interpretation = None
            suggested_next_action = None
            if exit_code_meaning == "timeout":
                interpretation = "命令超时，可能节点负载过高或 terminal_bridge 未连接"
                suggested_next_action = "请检查目标节点的可达性，或尝试执行低负载命令/查看日志"
                # T4-2: 记录超时计数
                from app.services.metrics import AGENT_TOOL_TIMEOUT_TOTAL

                AGENT_TOOL_TIMEOUT_TOTAL.labels(tool_name=tool_name).inc()
            elif exit_code != 0:
                interpretation = f"命令执行失败 (退出码: {exit_code})"
                if exit_code_meaning == "command_not_found":
                    interpretation += ": 找不到指定的命令/可执行文件"
                    suggested_next_action = "请检查命令拼写或该节点上是否安装了相应工具"
                elif exit_code_meaning == "permission_denied":
                    interpretation += ": 权限被拒绝，无法执行此操作"
                    suggested_next_action = "请检查执行用户的权限或使用 sudo"
                else:
                    suggested_next_action = "请根据错误日志提示修正命令参数，或重试"

            return cls(
                tool_name=tool_name,
                exec_id=exec_id,
                success=(exit_code == 0 and error is None),
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                exit_code_meaning=exit_code_meaning,
                truncated=truncated,
                interpretation=interpretation,
                suggested_next_action=suggested_next_action,
            )

        if isinstance(result, dict):
            # SOP / variable request dict
            success = error is None and not result.get("error")
            exit_code = 0 if success else -1
            stdout = json.dumps(result, ensure_ascii=False)
            stderr = error or result.get("error", "")
            return cls(
                tool_name=tool_name,
                exec_id=exec_id,
                success=success,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                truncated=False,
            )

        success = error is None
        exit_code = 0 if success else -1
        stdout = str(result) if result is not None else ""
        stderr = error or ""
        return cls(
            tool_name=tool_name,
            exec_id=exec_id,
            success=success,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=False,
        )


# 硬限制：最大推理步骤数，防止无限循环
MAX_STEPS = 40


# ─── Protocol 定义────────────────────


@runtime_checkable
class ToolExecutor(Protocol):
    """工具执行后端协议（SCPClient、AcliClient 等实现此协议）"""

    async def execute(self, tool_name: str, args: dict) -> Any: ...


@runtime_checkable
class ConfirmServiceProtocol(Protocol):
    """人工确认服务协议"""

    async def request_confirm(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
        exec_id: str | None = None,
    ): ...


@runtime_checkable
class AuditServiceProtocol(Protocol):
    """审计日志服务协议"""

    async def write(self, audit_id: str, **kwargs) -> None: ...


def _sanitize_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清理不完整的 tool_calls/tool 消息对。

    OpenAI API 要求每个 assistant 消息中的 tool_calls 必须有对应的 tool 消息。
    前一轮中断（如用户取消、网络错误）可能导致对话历史中残留不配对的消息。
    """
    if not messages:
        return messages

    clean: list[dict[str, Any]] = []
    pending_tool_call_ids: set[str] = set()

    for msg in messages:
        role = msg.get("role", "")

        if role == "assistant" and msg.get("tool_calls"):
            # 收集当前 assistant 消息中所有 tool_call_id
            pending_tool_call_ids = {
                tc.get("id", "") for tc in msg.get("tool_calls", []) if tc.get("id")
            }
            clean.append(msg)

        elif role == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id in pending_tool_call_ids:
                pending_tool_call_ids.discard(tc_id)
                clean.append(msg)
            # 如果 tool_call_id 不在待处理集合中（孤立 tool 消息），跳过

        else:
            # user/system 消息：如果有未配对的 tool_calls，移除最后一条 assistant 消息
            if pending_tool_call_ids:
                # 回退：移除未完整配对的 assistant tool_calls 消息
                while clean and clean[-1].get("role") == "assistant" and clean[-1].get("tool_calls"):
                    removed = clean.pop()
                    removed_ids = {tc.get("id", "") for tc in removed.get("tool_calls", []) if tc.get("id")}
                    pending_tool_call_ids -= removed_ids
                pending_tool_call_ids.clear()
            clean.append(msg)

    # 末尾残留：最后一条是带 tool_calls 的 assistant 但无后续 tool 消息
    if pending_tool_call_ids and clean and clean[-1].get("role") == "assistant" and clean[-1].get("tool_calls"):
        clean.pop()

    return clean


class ReactEngine:
    """ReAct 循环执行引擎"""

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        tool_registry: dict,  # TOOL_REGISTRY 字典
        tool_executor: ToolExecutor,
        confirm_service: ConfirmServiceProtocol | None = None,
        audit_service: AuditServiceProtocol | None = None,
        fact_store: Any = None,
        db_session_factory: Any = None,
        conversation_service_url: str = "",  # conversation-service 内部 URL，用于工具历史持久化
        internal_token: str = "",  # 内部服务认证 Token
    ) -> None:
        self._ai_registry = ai_registry
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._confirm_service = confirm_service
        self._audit = audit_service
        self._fact_store = fact_store
        self._db_session_factory = db_session_factory
        self._conversation_service_url = conversation_service_url
        self._internal_token = internal_token
        self.schema_validation_failed = False
        self.has_write_operation = False
        self.has_verification_after_write = False

    async def _load_prompt(self, name: str, placeholders: list[str]) -> str:
        """从 prompt 管理（system_prompt 表）加载 Prompt 模板。

        生产环境经 db_session_factory 走 StrictPromptLoader（DB 缺失则按设计 fail-loud）；
        DB 会话工厂为空（如部分单测）时回退到 create_mock_session_factory 的基准模板，
        保证逻辑路径与线上一致且单测可解析。
        """
        from shared.utils.prompt_loader import StrictPromptLoader, create_mock_session_factory

        factory = self._db_session_factory or create_mock_session_factory()
        async with factory() as session:
            return await StrictPromptLoader.load_and_validate(session, name, placeholders)

    async def execute(
        self,
        *,
        session_id: str,
        system_prompt: str,
        messages: list[dict[str, Any]],
        assistant_type: str = "htp-agent",
        case_id: str = "",
        user_id: str = "",
        max_iterations: int = MAX_STEPS,
        require_all_confirm: bool = False,
        execution_mode: str = "safe-only",
        extra_tools: list[dict] | None = None,  # T-AGT-22: 动态注入工具（仅本次 execute 有效）
        tool_executor: ToolExecutor | None = None,  # T-AGT-22: 可替换工具执行器（用于 SOP 工具注入上下文）
        sop_mode: bool = False,  # DC-01: SOP 模式，用于注入 SOP 导航工具到 LLM tool list
        response_schema: type[BaseModel] | None = None,  # T3-2: 结构化输出 Schema
    ) -> AsyncGenerator[AgentEvent, None]:
        """ReAct 循环（Reason → Act → Observe）

        Args:
            session_id: 会话 ID
            system_prompt: 系统提示词（已注入知识）
            messages: OpenAI 格式消息列表
            assistant_type: 助手类型标识
            case_id: 工单 ID
            user_id: 用户 ID
            max_iterations: 最大循环次数
            require_all_confirm: True 时所有工具调用（包括只读工具）均需确认
            extra_tools: 动态注入的工具列表（OpenAI function calling 格式），仅本次 execute 有效
            tool_executor: 可替换的工具执行器（用于 SOP 工具注入上下文），默认使用实例初始化时的执行器
            sop_mode: 是否是 SOP 模式
            response_schema: 结构化输出 Schema (Pydantic BaseModel)

        Yields:
            AgentStageUpdate: 推理阶段状态（thinking、executing）
            AgentInteractiveRequest: 确认请求（如需要）
            AgentTextChunk: 最终文本回复
        """
        from shared.clients.ai_client import InvokeResult

        ai_client = self._ai_registry.get_client(assistant_type)
        if not ai_client:
            yield AgentTextChunk(content="[错误] 未找到 AI 客户端")
            return

        # Langfuse: 创建顶层 agent execution span，串联后续所有 LLM 调用与工具执行
        agent_obs, agent_obs_ctx = start_agent_observation(
            user_id=user_id,
            case_id=case_id,
            assistant_type=assistant_type,
            execution_mode=execution_mode,
            sop_mode=sop_mode,
            max_iterations=max_iterations,
        )
        total_steps = 0

        def _end_agent_obs():
            """结束 Langfuse agent execution observation。在 execute() 所有退出路径上调用。"""
            if agent_obs:
                with suppress(Exception):
                    agent_obs.update(metadata={"total_steps": total_steps})
            if agent_obs_ctx:
                agent_obs_ctx.__exit__(None, None, None)

        # T3-3/DC-05/DC-06: 统一输出约束（精简合并，减少 token 浪费）
        # T3-3/DC-05/DC-06/DC-08: 统一输出约束
        # 该输出约束块已数据库化（prompt 管理 → s1_react_output_constraint_v1），按阶段统一纳管
        output_constraint = await self._load_prompt("s1_react_output_constraint_v1", [])
        system_prompt += "\n\n" + output_constraint

        if response_schema:
            schema_json = json.dumps(response_schema.model_json_schema(), ensure_ascii=False)
            # 该结构化输出要求前缀已数据库化（prompt 管理 → s1_react_structured_output_v1），占位符 schema_json 动态注入
            structured_output = await self._load_prompt("s1_react_structured_output_v1", ["schema_json"])
            system_prompt += "\n\n" + structured_output.format(schema_json=schema_json)

        # 工作消息列表（在循环中动态追加）
        # 清理对话历史中不完整的 tool_calls/tool 配对，OpenAI API 要求每个
        # tool_call_id 必须有对应的 tool 消息，否则返回 400 错误
        clean_messages = _sanitize_tool_messages(messages)
        work_messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *clean_messages,
        ]

        # 工具列表（OpenAI function calling 格式）+ 动态注入工具（T-AGT-22）
        tools = await self._get_tools_for_llm(extra_tools=extra_tools, sop_mode=sop_mode)

        # T-AGT-22: 使用传入的 tool_executor 或实例默认执行器
        active_tool_executor = tool_executor or self._tool_executor

        for step_count in range(1, max_iterations + 1):
            # ── 推理阶段 ──────────────────────────────────────────────────────
            yield AgentStageUpdate(
                stage="thinking",
                metadata={"step": step_count, "message": "正在分析..."},
            )

            # 非流式 invoke()：支持 tool_calls 解析
            try:
                invoke_result: InvokeResult = await ai_client.invoke(
                    messages=work_messages,
                    tools=tools,
                    user_id=session_id,
                    case_id=case_id,
                    temperature=settings.LLM_TEMPERATURE_REACT,
                )
            except Exception as exc:
                logger.error(
                    event="react_invoke_error",
                    error=str(exc),
                    step=step_count,
                    session_id=session_id,
                )
                total_steps = step_count
                yield AgentTextChunk(content=f"[错误] LLM 调用失败：{exc}")
                _end_agent_obs()
                return

            # ── 终止条件：LLM 给出文字回复 ─────────────────────────────────
            if invoke_result.content is not None:
                # T3-5: 校验优先强绑定约束检测
                if self.has_write_operation and not self.has_verification_after_write:
                    closure_keywords = [
                        "已恢复",
                        "已解决",
                        "修复",
                        "成功",
                        "搞定",
                        "完成",
                        "正常",
                        "恢复正常",
                        "排障结束",
                        "closure",
                        "resolved",
                        "fixed",
                        "success",
                    ]
                    if any(kw in invoke_result.content for kw in closure_keywords):
                        logger.warning("校验优先闭环拦截: 宣称完成但未验证")
                        try:
                            from app.services.metrics import AGENT_VERIFICATION_BLOCKED_TOTAL

                            AGENT_VERIFICATION_BLOCKED_TOTAL.inc()
                        except Exception as met_err:
                            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")
                        block_msg = {
                            "role": "system",
                            "content": (
                                "【校验优先闭环强制拦截】\n"
                                "你刚刚执行了修复性写操作命令，但在宣称修复完成/定位结束前，你必须先执行验证状态工具（例如 get_active_alerts，虚拟机状态检查，或者 status 检查等）"
                                "来验证系统当前状态，证实问题确实已解决。严禁跳过验证直接给出排障报告。"
                            ),
                        }
                        work_messages.append(block_msg)
                        continue

                # T3-4: 运行轻量级幻觉检测器并支持 Re-run 一次
                from app.services.hallucination_detector import HallucinationDetector

                detector = HallucinationDetector(tool_registry=self._tool_registry)

                tool_results_list = []
                executed_tool_names = []
                for msg in work_messages:
                    if msg.get("role") == "tool":
                        tool_results_list.append(msg.get("content", ""))
                    if msg.get("role") == "assistant" and msg.get("tool_calls"):
                        for tc in msg["tool_calls"]:
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                executed_tool_names.append(fn["name"])

                detection_report = detector.detect(
                    llm_text=invoke_result.content, executed_tools=executed_tool_names, tool_outputs=tool_results_list
                )

                if detection_report.get("has_hallucination"):
                    # 若 LLM 输出已包含明确的诊断结论标记，说明推理已完成，
                    # 幻觉检测的假阳性（如引用了来自 SOP 上下文而非工具执行的数据源）
                    # 不应触发 re-run。re-run 会增加 5-15s 延迟且常产出更差的输出。
                    conclusion_markers = [
                        "排障结论", "根因已确认", "根因确认", "排障闭环",
                        "修复方案", "诊断结论", "根因定位",
                    ]
                    has_conclusion = any(
                        m in (invoke_result.content or "") for m in conclusion_markers
                    )
                    if has_conclusion:
                        logger.info(
                            "hallucination_rerun_skipped",
                            "检测到幻觉但输出已包含结论，跳过 re-run，由二次检测追加警告",
                        )
                    else:
                        logger.warning(
                            "hallucination_detected_before_report", "最终报告生成前检测到幻觉，尝试重新生成一次 (Re-run)..."
                        )
                        try:
                            # 反幻觉自我检查指令已数据库化（prompt 管理 → s4_react_antihallucination_v1）
                            antihallucination_prompt = await self._load_prompt(
                                "s4_react_antihallucination_v1", []
                            )
                            temp_messages = work_messages + [
                                {"role": "assistant", "content": invoke_result.content},
                                {
                                    "role": "system",
                                    "content": antihallucination_prompt,
                                },
                            ]
                            new_invoke_result = await ai_client.invoke(
                                messages=temp_messages,
                                tools=tools,
                                user_id=session_id,
                                case_id=case_id,
                                temperature=settings.LLM_TEMPERATURE_REACT,
                            )
                            if new_invoke_result.content is not None:
                                logger.info("rerun_success", "Re-run 重新生成成功")
                                invoke_result = new_invoke_result
                        except Exception as re_exc:
                            logger.warning("rerun_failed", f"Re-run 失败: {re_exc}")

                # T3-2: 校验 Schema
                if response_schema:
                    cleaned_json = self._extract_json(invoke_result.content)
                    try:
                        parsed = response_schema.model_validate_json(cleaned_json)
                        logger.info(
                            "schema_validation_success", f"结构化输出校验成功: schema={response_schema.__name__}"
                        )
                        try:
                            from app.services.metrics import AGENT_SCHEMA_VALIDATION_TOTAL

                            AGENT_SCHEMA_VALIDATION_TOTAL.labels(
                                schema_name=response_schema.__name__, status="success"
                            ).inc()
                        except Exception as met_err:
                            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")
                        if response_schema.__name__ == "ClaimVerification" and self._fact_store:
                            await self._fact_store.write_claim_verification(session_id, parsed)
                        # T4-2: ReasoningOutput 置信度与无证据结论指标
                        if response_schema.__name__ == "ReasoningOutput":
                            try:
                                from app.services.metrics import (
                                    AGENT_REASONING_CONFIDENCE,
                                    AGENT_UNSUPPORTED_CLAIM_TOTAL,
                                )

                                if hasattr(parsed, "hypotheses") and parsed.hypotheses:
                                    avg_conf = sum(h.confidence for h in parsed.hypotheses) / len(parsed.hypotheses)
                                    AGENT_REASONING_CONFIDENCE.set(avg_conf)
                                if hasattr(parsed, "unsupported_claims") and parsed.unsupported_claims:
                                    for _claim in parsed.unsupported_claims:
                                        AGENT_UNSUPPORTED_CLAIM_TOTAL.labels(claim_type="reasoning_output").inc()
                            except Exception as met_err:
                                logger.warning("metrics_record_failed", f"记录 ReasoningOutput metrics 失败: {met_err}")
                    except Exception as e:
                        logger.warning(
                            "schema_validation_failed",
                            f"结构化输出校验失败: schema={response_schema.__name__}, error={e}, raw={invoke_result.content}",
                        )
                        self.schema_validation_failed = True
                        try:
                            from app.services.metrics import AGENT_SCHEMA_VALIDATION_TOTAL

                            AGENT_SCHEMA_VALIDATION_TOTAL.labels(
                                schema_name=response_schema.__name__, status="failed"
                            ).inc()
                        except Exception as met_err:
                            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")

                # 流式输出最终文字回复（直接从 invoke_result.content 分块 yield，
                # 不再重复调用 LLM 流式 API。invoke() 已经返回了完整的诊断结论文本，
                # 无需为了分块显示再发起一次冗余的流式调用。这不仅浪费 tokens 和耗时，
                # 还可能导致 LLM 在流式模式下输出 tool_call XML 而非文本，让用户看到无效内容。
                # 工具不是目的，根因解决方案才是目的。）
                final_content = invoke_result.content or ""
                chunk_size = 80
                for i in range(0, len(final_content), chunk_size):
                    chunk = final_content[i : i + chunk_size]
                    yield AgentTextChunk(content=chunk)
                    await asyncio.sleep(0.01)  # 模拟流式打字体验

                # 流式输出后二次幻觉检测：若 re-run 前检测到幻觉并已触发重新生成，
                # 此处对 re-run 后的内容再做一次检测，有残留幻觉时追加警告提示。
                final_text = invoke_result.content or ""
                detection_report = detector.detect(
                    llm_text=final_text, executed_tools=executed_tool_names, tool_outputs=tool_results_list
                )

                if detection_report.get("has_hallucination"):
                    reasons = detection_report.get("reasons", [])
                    warning_msg = f'\n\n*(注：本回复中部分内容存在高风险幻觉（如：{", ".join(reasons)}），已标注为"待验证"，请工程师注意确认)*'
                    yield AgentTextChunk(content=warning_msg)
                    try:
                        from app.services.metrics import (
                            AGENT_HALLUCINATION_DETECTED_TOTAL,
                            AGENT_UNSUPPORTED_CLAIM_TOTAL,
                        )

                        for htype, hkey in [
                            ("phantom_tools", "phantom_tool"),
                            ("overconfident_claims", "overconfident"),
                            ("ungrounded_numbers", "ungrounded_number"),
                        ]:
                            if detection_report.get(htype):
                                AGENT_HALLUCINATION_DETECTED_TOTAL.labels(hallucination_type=hkey).inc()
                        # T4-2: 同时记录无证据结论指标（用于 agent_unsupported_claim_total 面板）
                        if detection_report.get("overconfident_claims"):
                            AGENT_UNSUPPORTED_CLAIM_TOTAL.labels(claim_type="overconfident").inc(
                                len(detection_report["overconfident_claims"])
                            )
                        if detection_report.get("ungrounded_numbers"):
                            AGENT_UNSUPPORTED_CLAIM_TOTAL.labels(claim_type="ungrounded_number").inc(
                                len(detection_report["ungrounded_numbers"])
                            )
                    except Exception as met_err:
                        logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")

                # T4-2: 记录推理步数（成功完成）
                try:
                    from app.services.metrics import AGENT_REASONING_STEPS_TOTAL, AGENT_RESOLUTION_STEPS

                    AGENT_REASONING_STEPS_TOTAL.labels(session_id=session_id, case_id=case_id or "unknown").inc(
                        step_count
                    )
                    AGENT_RESOLUTION_STEPS.observe(step_count)
                except Exception as met_err:
                    logger.warning("metrics_record_failed", f"记录步数 metrics 失败: {met_err}")

                total_steps = step_count
                _end_agent_obs()
                return

            # ── 工具调用轮次 ──────────────────────────────────────────────────
            if not invoke_result.tool_calls:
                # invoke() 返回空（不应发生），安全退出
                logger.warning(
                    event="react_empty_result",
                    step=step_count,
                    session_id=session_id,
                )
                total_steps = step_count
                yield AgentTextChunk(content="诊断推理已完成。")
                _end_agent_obs()
                return

            # 将 assistant tool_calls 消息追加到历史
            assistant_msg: dict = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            # BUG-FIX(DC-02): 必须用 json.dumps() 而非 str()。
                            # str(dict) 产生 Python 单引号格式（非法 JSON），会导致
                            # Volcengine/OpenAI 等严格 Schema 校验的上游返回 400 BadRequest。
                            "arguments": json.dumps(tc.arguments or {}),
                        },
                    }
                    for tc in invoke_result.tool_calls
                ],
            }
            work_messages.append(assistant_msg)

            # 逐个执行工具调用
            for tc in invoke_result.tool_calls:
                tool_call_dict = {"id": tc.id, "name": tc.name, "args": tc.arguments}

                # T3-5: 更新验证追踪标记
                temp_tool_def = self._tool_registry.get(tc.name)
                if not temp_tool_def:
                    from app.adapters.agents.htp.tool_registry import (
                        TOOL_REGISTRY_MANAGER,
                        refresh_tool_registry_if_needed,
                    )

                    runtime_registry = (
                        await refresh_tool_registry_if_needed()
                        if TOOL_REGISTRY_MANAGER is not None
                        else self._tool_registry
                    )
                    temp_tool_def = runtime_registry.get(tc.name)
                temp_risk = temp_tool_def.risk_level if temp_tool_def else 1
                if temp_risk >= 2:
                    self.has_write_operation = True
                    self.has_verification_after_write = False
                elif self.has_write_operation and tc.name not in (
                    "get_sop_node",
                    "sop_advance",
                    "sop_request_variable",
                ):
                    self.has_verification_after_write = True

                # require_all_confirm 覆盖：将只读工具也升级为需确认
                tool_result = None
                tool_error = None
                tool_exec_id = tc.id
                async for event in self._execute_tool_call(
                    tool_call=tool_call_dict,
                    session_id=session_id,
                    step=step_count,
                    require_all_confirm=require_all_confirm,
                    tool_executor=active_tool_executor,  # T-AGT-22: 传入执行器
                    execution_mode=execution_mode,  # T1-3: 传入执行模式
                    case_id=case_id,
                ):
                    # 捕获工具执行结果
                    if isinstance(event, ToolResultEvent):
                        tool_result = event.result
                        tool_error = event.error
                        if event.exec_id:
                            tool_exec_id = event.exec_id
                    # AgentTextChunk 需要传递给外层（如"操作已取消"、"确认服务暂不可用"）。
                    # 仅用户取消/服务不可用才终止循环；工具执行报错（如"命令执行失败"）
                    # 不应终止，应让 LLM 拿到错误结果后自我修正继续推理。
                    elif isinstance(event, AgentTextChunk):
                        yield event
                        if "操作已取消" in event.content or "已中止" in event.content:
                            total_steps = step_count
                            _end_agent_obs()
                            return
                    else:
                        yield event

                # 将工具结果以 ToolResultEnvelope 的结构化形式追加到消息历史 (T0-1)
                envelope = ToolResultEnvelope.from_raw_result(
                    tool_name=tc.name,
                    exec_id=tool_exec_id,
                    result=tool_result,
                    error=tool_error,
                )
                tool_result_content = envelope.to_llm_message()
                tool_result_dict = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result_content,
                }
                work_messages.append(tool_result_dict)

                # ReAct 工具调用历史跨轮次持久化（fire-and-forget）
                # 将 assistant tool_calls 消息和 tool result 消息持久化到 conversation-service，
                # 当用户点击"继续"时大模型可通过完整的 messages[] 无缝续接 ReAct 推理链。
                if self._conversation_service_url and session_id and case_id:
                    asyncio.create_task(
                        self._persist_tool_turn(
                            session_id=session_id,
                            case_id=case_id,
                            tool_calls_msg=assistant_msg,  # 含 tool_calls 数组的 assistant 消息
                            tool_result_msg=tool_result_dict,  # tool result 消息
                            exec_id=tool_exec_id,
                        )
                    )

        # 超出步数限制
        total_steps = max_iterations
        yield AgentTextChunk(content="⚠️ 诊断步骤已达上限，请联系人工支持。")

        # Langfuse: 结束 agent execution span
        _end_agent_obs()
        return

    @staticmethod
    def _extract_json(text: str) -> str:
        """从 LLM 输出中提取 JSON 片段（去除 <reasoning> 标签）。"""
        text_clean = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
        match = re.search(r"```json\s*(.*?)\s*```", text_clean, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        match = re.search(r"```\s*(.*?)\s*```", text_clean, re.DOTALL)
        if match:
            return match.group(1).strip()
        start = text_clean.find("{")
        end = text_clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text_clean[start : end + 1].strip()
        return text_clean.strip()

    async def _get_tools_for_llm(self, extra_tools: list[dict] | None = None, sop_mode: bool = False) -> list[dict]:
        """返回 OpenAI function calling 格式 of 工具列表（排除高危工具）。

        Args:
            extra_tools: 动态注入的工具列表（T-AGT-22），追加到默认工具列表末尾
            sop_mode: 是否是 SOP 模式，如果是 SOP 模式则包含 SOP 导航工具

        Returns:
            工具列表（OpenAI function calling 格式）
        """
        from app.adapters.agents.htp.tool_registry import (
            TOOL_REGISTRY_MANAGER,
            get_tools_for_llm_from_registry,
            refresh_tool_registry_if_needed,
        )

        registry = await refresh_tool_registry_if_needed() if TOOL_REGISTRY_MANAGER is not None else self._tool_registry
        self._tool_registry = registry
        base_tools = get_tools_for_llm_from_registry(registry, include_sop=sop_mode)
        if extra_tools:
            # 合并动态工具（追加到末尾，LLM 可选择使用）
            return base_tools + extra_tools
        return base_tools

    async def _execute_tool_call(
        self,
        tool_call: dict,
        session_id: str,
        step: int,
        require_all_confirm: bool = False,
        tool_executor: ToolExecutor | None = None,  # T-AGT-22: 可替换执行器
        execution_mode: str = "safe-only",  # T1-3: 执行模式（off/safe-only/aggressive）
        case_id: str = "",
    ) -> AsyncGenerator[AgentEvent, None]:
        """执行单个工具调用，含授权检查和审计记录

        Args:
            tool_call: 工具调用信息 {"id": "...", "name": "...", "args": {...}}
            session_id: 会话 ID
            step: 当前步骤数
            require_all_confirm: True 时只读工具也升级为需要用户确认（S5 修复模式用）
            tool_executor: 可替换的工具执行器（T-AGT-22，用于 SOP 工具注入上下文）

        Yields:
            AgentStageUpdate: 工具执行状态
            AgentInteractiveRequest: 确认请求（如需要）
            AgentTextChunk: 工具执行结果（可选）
        """
        from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY_MANAGER, refresh_tool_registry_if_needed
        from app.tools.acli.classifier import classify_acli, classify_bash, risk_to_policy
        from app.tools.acli.semantic_validator import ToolSemanticValidator

        active_tool_registry = (
            await refresh_tool_registry_if_needed() if TOOL_REGISTRY_MANAGER is not None else self._tool_registry
        )
        self._tool_registry = active_tool_registry

        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("args", {})
        exec_id = str(uuid.uuid4())

        # T3-2: 降级拦截 - 如果前序推理格式校验失败，禁止执行高风险写操作工具
        if getattr(self, "schema_validation_failed", False):
            tool_def = active_tool_registry.get(tool_name)
            risk = tool_def.risk_level if tool_def else 1
            if risk >= 2:
                logger.warning(
                    "schema_validation_block_write", f"降级拦截: Schema 校验失败，禁止执行高风险写操作工具: {tool_name}"
                )
                yield ToolResultEvent(
                    tool_name=tool_name,
                    exec_id=exec_id,
                    result={"error": f"由于前序推理格式校验失败，已降级拦截高风险写操作工具 {tool_name} 的执行。"},
                )
                return

        # T-AGT-22: 使用传入的 tool_executor 或实例默认执行器
        active_executor = tool_executor or self._tool_executor

        # T1-4: 写入初始 proposed 状态审计记录（用于挂载 exec_id 事务基准）
        if self._audit:
            try:
                import hashlib

                from shared.observability.otel import get_current_trace_id

                input_str = json.dumps(tool_args, sort_keys=True, ensure_ascii=False)
                input_hash = hashlib.sha256(input_str.encode("utf-8")).hexdigest()

                temp_tool_def = active_tool_registry.get(tool_name)
                temp_risk = temp_tool_def.risk_level if temp_tool_def else 1
                temp_policy = temp_tool_def.policy if temp_tool_def else "auto"

                await self._audit.write(
                    audit_id=exec_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    risk_level=temp_risk,
                    policy=temp_policy,
                    result=None,
                    error=None,
                    started_at=datetime.now(UTC),
                    completed_at=None,
                    duration_ms=None,
                    trace_id=get_current_trace_id(),
                    step=step,
                    status="proposed",
                    input_hash=input_hash,
                )
            except Exception as e:
                logger.error(f"写入 proposed 状态审计失败: {e}")

        tool_def = active_tool_registry.get(tool_name)
        if tool_def:
            # 运行时契约门禁：数据库 tool_definition 更新后若 usage_template 与 parameters 不一致，
            # 在工具执行前拦截，避免重启后才暴露。仅检查被调用的工具，不影响其他工具。
            try:
                from app.adapters.agents.htp.tool_registry import verify_tool_contract
                verify_tool_contract(tool_def)
            except Exception as contract_err:
                logger.error(
                    event="tool_contract_runtime_failed",
                    tool_name=tool_name,
                    error=str(contract_err),
                )
                yield ToolResultEvent(
                    tool_name=tool_name,
                    exec_id=exec_id,
                    result={"error": f"工具 {tool_name} 契约校验失败: {contract_err}"},
                    error="contract_verification_failed",
                )
                return

        if not tool_def:
            # T0-1：未知工具路径统一走 ToolResultEnvelope，让 LLM 能拿到结构化错误并自我纠正
            envelope = ToolResultEnvelope(
                tool_name=tool_name,
                exec_id=exec_id,
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"未知工具: {tool_name}，请检查工具名称是否正确，或联系管理员添加工具定义",
                exit_code_meaning="unknown_tool",
                interpretation="请求的工具未在 TOOL_REGISTRY 中定义，LLM 需检查拼写或调整策略",
                suggested_next_action="请检查工具名称拼写，或改用已知工具继续诊断",
            )
            yield ToolResultEvent(
                tool_name=tool_name,
                exec_id=exec_id,
                result=envelope.to_llm_message(),
                error="unknown_tool",
            )
            return

        # T0-5: ToolCallValidator 参数前置校验
        is_valid, err_msg = ToolCallValidator.validate(tool_name, tool_args, tool_def.parameters)
        if not is_valid:
            logger.warning(
                event="tool_call_validation_failed",
                tool_name=tool_name,
                args=tool_args,
                error=err_msg,
            )
            # T1-4: 更新为 failed 状态
            if self._audit:
                try:
                    await self._audit.write(
                        audit_id=exec_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        risk_level=tool_def.risk_level,
                        policy=tool_def.policy,
                        result=None,
                        error=err_msg,
                        status="failed",
                    )
                except Exception as e:
                    logger.error(f"更新 failed 状态审计失败: {e}")

            yield AgentStageUpdate(
                stage="tool_call",
                metadata={
                    "exec_id": exec_id,
                    "tool_name": tool_name,
                    "args": tool_args,
                    "risk_level": tool_def.risk_level,
                    "status": "failed",
                },
            )
            # 广播 tool_result 结果事件，防止前端卡在"正在等待输出..."
            yield AgentStageUpdate(
                stage="tool_result",
                metadata={
                    "exec_id": exec_id,
                    "tool_name": tool_name,
                    "status": "failed",
                    "result": None,
                    "error": err_msg,
                    "duration_ms": 0,
                },
            )
            # 返回校验失败的错误结果给 LLM
            yield ToolResultEvent(
                result=f"[error] 参数校验失败：{err_msg}。请修正参数后重新尝试调用该工具。",
                tool_name=tool_name,
                error=err_msg,
                exec_id=exec_id,
            )
            return

        semantic_result = ToolSemanticValidator.validate(tool_name, tool_args, tool_def)
        if not semantic_result.ok:
            err_msg = semantic_result.to_feedback(tool_name)
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
            try:
                from shared.observability.otel import get_current_trace_id

                trace_id = get_current_trace_id() or "unknown"
            except Exception:
                trace_id = "unknown"
            logger.warning(
                event="tool_call_semantic_validation_failed",
                exec_id=exec_id,
                session_id=session_id,
                tool_name=tool_name,
                args=tool_args,
                validation_codes=validation_codes,
                trace_id=trace_id,
            )
            if self._audit:
                try:
                    await self._audit.write(
                        audit_id=exec_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        risk_level=tool_def.risk_level,
                        policy=tool_def.policy,
                        result=None,
                        error=err_msg,
                        status="failed",
                    )
                except Exception as e:
                    logger.error(f"更新 failed 状态审计失败: {e}")

            yield AgentStageUpdate(
                stage="tool_result",
                metadata={
                    "exec_id": exec_id,
                    "tool_name": tool_name,
                    "status": "validation_failed",
                    "result": None,
                    "error": err_msg,
                    "duration_ms": 0,
                },
            )
            yield ToolResultEvent(
                result=err_msg,
                tool_name=tool_name,
                error="semantic_validation_failed",
                exec_id=exec_id,
            )
            return

        # ─── 动态风险覆盖（仅对通用命令执行工具）───
        # T-TOOL-15: 对 acli_exec 和 bash_exec 工具动态计算风险等级
        if tool_name in ("acli_exec", "bash_exec"):
            command = tool_args.get("command", "")
            runtime_risk = classify_acli(command) if tool_name == "acli_exec" else classify_bash(command)
            # 使用 model_copy 创建副本并更新风险等级（Pydantic v2）
            tool_def = tool_def.model_copy(
                update={
                    "risk_level": runtime_risk,
                    "policy": risk_to_policy(runtime_risk),
                }
            )
            logger.info(
                event="dynamic_risk_override",
                tool_name=tool_name,
                command=command,
                runtime_risk=runtime_risk,
                policy=tool_def.policy,
            )
            # 高危命令直接阻止执行
            if tool_def.policy == "block":
                # T1-4: 更新状态为 cancelled
                if self._audit:
                    try:
                        await self._audit.write(
                            audit_id=exec_id,
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            risk_level=tool_def.risk_level,
                            policy=tool_def.policy,
                            result=None,
                            error=f"[blocked] 命令 {command!r} 属于高危操作（risk=3），已拒绝执行。",
                            status="cancelled",
                        )
                    except Exception as e:
                        logger.error(f"更新 cancelled 状态审计失败: {e}")

                # T0-1：动态策略拦截路径统一走 ToolResultEnvelope，让 LLM 能拿到结构化错误
                envelope = ToolResultEnvelope(
                    tool_name=tool_name,
                    exec_id=exec_id,
                    success=False,
                    exit_code=-1,
                    stdout="",
                    stderr=f"[blocked] 命令 {command!r} 属于高危操作（risk=3），已拒绝执行。",
                    exit_code_meaning="blocked_by_policy",
                    truncated=False,
                    interpretation="该工具被动态策略评估为高危操作（risk=3），系统已自动拦截",
                    suggested_next_action="如确需执行，请联系管理员调整工具 policy 配置，或改用低风险替代方案",
                )
                yield ToolResultEvent(
                    result=envelope,
                    exec_id=exec_id,
                    tool_name=tool_name,
                )
                return
        # ─────────────────────────────────────────

        # 高危工具（risk_level=3 / policy=block）直接拒绝
        if tool_def.policy == "block":
            # T1-4: 更新状态为 cancelled
            if self._audit:
                try:
                    await self._audit.write(
                        audit_id=exec_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        risk_level=tool_def.risk_level,
                        policy=tool_def.policy,
                        result=None,
                        error=f"工具 {tool_name} 风险等级过高（risk=3），已阻止执行。",
                        status="cancelled",
                    )
                except Exception as e:
                    logger.error(f"更新 cancelled 状态审计失败: {e}")

            yield AgentStageUpdate(
                stage="tool_call",
                metadata={
                    "exec_id": exec_id,
                    "tool_name": tool_name,
                    "args": tool_args,
                    "risk_level": tool_def.risk_level,
                    "status": "blocked",
                },
            )
            # T0-1：risk=3 block 路径统一走 ToolResultEnvelope，让 LLM 能拿到结构化错误
            envelope = ToolResultEnvelope(
                tool_name=tool_name,
                exec_id=exec_id,
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"工具 {tool_name} 风险等级过高（risk=3），已按安全策略阻止执行",
                exit_code_meaning="blocked_by_policy",
                interpretation="该工具被标记为高危操作（policy=block），系统已自动拦截",
                suggested_next_action="如确需执行，请联系管理员调整工具 policy 配置，或改用低风险替代方案",
            )
            yield ToolResultEvent(
                tool_name=tool_name,
                exec_id=exec_id,
                result=envelope.to_llm_message(),
                error="blocked_by_policy",
            )
            return

        # T1-3: 调用服务端安全策略进行评估，限制高危命令的自动执行条件
        from app.services.policy_service import PolicyService

        policy_service = PolicyService()
        # T1-3 修复：将「策略判定」与「确认服务可用性」解耦。
        # 旧实现 `needs_confirm = ... and self._confirm_service` 会在 confirm_service 缺失时
        # 把 needs_confirm 降为 False，导致 risk≥2 的高危工具被直接执行（fail-open）。
        # 新实现：先单独计算策略判定，再在策略要求确认但 service 不可用时 fail-closed 拒绝执行。
        policy_requires_confirm = policy_service.evaluate_needs_confirm(
            tool_name=tool_name,
            risk_level=tool_def.risk_level,
            require_all_confirm=require_all_confirm,
            execution_mode=execution_mode,
        )
        if policy_requires_confirm and not self._confirm_service:
            logger.error(
                event="confirm_service_unavailable_fail_closed",
                message=f"风险等级 {tool_def.risk_level} 的工具 {tool_name} 需用户确认，但 ConfirmService 不可用，按 fail-closed 策略拒绝执行",
                exec_id=exec_id,
                session_id=session_id,
                tool_name=tool_name,
                risk_level=tool_def.risk_level,
            )
            if self._audit:
                try:
                    await self._audit.write(
                        audit_id=exec_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        risk_level=tool_def.risk_level,
                        policy=tool_def.policy,
                        status="failed",
                        error="confirm_service_unavailable",
                    )
                except Exception as e:
                    logger.error(f"写入 fail-closed 审计失败: {e}")
            envelope = ToolResultEnvelope(
                tool_name=tool_name,
                exec_id=exec_id,
                success=False,
                exit_code=-1,
                stdout="",
                stderr="ConfirmService 不可用，已按 fail-closed 安全策略拒绝执行高危工具",
                exit_code_meaning="confirm_service_unavailable",
                interpretation="服务端确认服务不可用，无法获得用户对高危操作的授权",
                suggested_next_action="请联系运维确认 Redis 与 ConfirmService 是否正常；或改用 risk_level<=1 的只读工具继续诊断",
            )
            # 在 _execute_tool_call 协程中只通过 yield ToolResultEvent 上抛工具结果，
            # 由上游 execute() 主循环负责将其追加到 work_messages 并回填给 LLM。
            yield ToolResultEvent(
                tool_name=tool_name,
                exec_id=exec_id,
                result=envelope.to_llm_message(),
                error="confirm_service_unavailable",
            )
            return
        needs_confirm = policy_requires_confirm and self._confirm_service is not None
        if needs_confirm:
            import hashlib
            from datetime import timedelta

            # 计算参数哈希，用于幂等和篡改验证
            input_str = json.dumps(tool_args, sort_keys=True, ensure_ascii=False)
            input_hash = hashlib.sha256(input_str.encode("utf-8")).hexdigest()

            # 120 秒后授权决策失效
            expires_at = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()

            # T1-4: 挂起确认时，将 status 更新为 confirm
            if self._audit:
                try:
                    await self._audit.write(
                        audit_id=exec_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        risk_level=tool_def.risk_level,
                        policy=tool_def.policy,
                        status="confirm",
                    )
                except Exception as e:
                    logger.error(f"更新 confirm 状态审计失败: {e}")

            # 1. 广播 tool_call 挂起确认事件
            yield AgentStageUpdate(
                stage="tool_call",
                metadata={
                    "exec_id": exec_id,
                    "tool_name": tool_name,
                    "args": tool_args,
                    "risk_level": tool_def.risk_level,
                    "status": "pending",
                    "input_hash": input_hash,
                },
            )

            # 2. 广播确认卡片
            yield AgentInteractiveRequest(
                request_id=exec_id,  # request_id 与 exec_id 保持一致
                acp_session_id=session_id,
                kind="tool_confirm",
                title=f"确认执行：{tool_name}",
                prompt=f"将执行操作：{tool_name}，参数：{tool_args}",
                options=[
                    {"optionId": "approved", "name": "确认执行"},
                    {"optionId": "rejected", "name": "取消"},
                ],
                custom_input=False,
                metadata={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "risk_level": tool_def.risk_level,
                    "step": step,
                },
                exec_id=exec_id,
                input_hash=input_hash,
                expires_at=expires_at,
            )

            try:
                confirm_result = await self._confirm_service.request_confirm(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    risk_level=tool_def.risk_level,
                    exec_id=exec_id,
                )
            except Exception as e:
                logger.error(f"确认服务异常: {e}")
                # T1-4: 确认异常，更新为 failed 状态
                if self._audit:
                    try:
                        await self._audit.write(
                            audit_id=exec_id,
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            risk_level=tool_def.risk_level,
                            policy=tool_def.policy,
                            error=str(e),
                            status="failed",
                        )
                    except Exception as audit_err:
                        logger.error(f"更新 failed 状态审计失败: {audit_err}")

                yield AgentStageUpdate(
                    stage="tool_call",
                    metadata={
                        "exec_id": exec_id,
                        "tool_name": tool_name,
                        "args": tool_args,
                        "risk_level": tool_def.risk_level,
                        "status": "failed",
                    },
                )
                yield AgentTextChunk(content=f"确认服务暂不可用，操作 {tool_name} 已中止")
                return

            if confirm_result.value != "approved":
                status_val = "cancelled" if confirm_result.value == "rejected" else "failed"
                err_val = "用户取消执行" if confirm_result.value == "rejected" else "等待授权确认超时"
                # T1-4: 用户取消或超时，更新状态为 cancelled 或 failed
                if self._audit:
                    try:
                        await self._audit.write(
                            audit_id=exec_id,
                            session_id=session_id,
                            tool_name=tool_name,
                            tool_args=tool_args,
                            risk_level=tool_def.risk_level,
                            policy=tool_def.policy,
                            error=err_val,
                            status=status_val,
                        )
                    except Exception as audit_err:
                        logger.error(f"更新 {status_val} 状态审计失败: {audit_err}")

                yield AgentStageUpdate(
                    stage="tool_call",
                    metadata={
                        "exec_id": exec_id,
                        "tool_name": tool_name,
                        "args": tool_args,
                        "risk_level": tool_def.risk_level,
                        "status": "cancelled",
                    },
                )
                yield AgentTextChunk(content="操作已取消")
                return

        # T1-4: 工具开始运行，更新状态为 executing
        if self._audit:
            try:
                await self._audit.write(
                    audit_id=exec_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    risk_level=tool_def.risk_level,
                    policy=tool_def.policy,
                    status="executing",
                )
            except Exception as e:
                logger.error(f"更新 executing 状态审计失败: {e}")

        # 广播 tool_call 开始运行事件
        yield AgentStageUpdate(
            stage="tool_call",
            metadata={
                "exec_id": exec_id,
                "tool_name": tool_name,
                "args": tool_args,
                "risk_level": tool_def.risk_level,
                "status": "running",
            },
        )

        # 只读且 policy=notify：执行前通知前端
        if tool_def.policy == "notify":
            yield AgentStageUpdate(
                stage="executing", metadata={"tool": tool_name, "args": tool_args, "message": "正在获取日志..."}
            )

        # 1. 检查熔断器状态
        from app.services.tool_reliability import ToolCircuitBreaker, ToolRetryPolicy

        breaker = ToolCircuitBreaker(tool_name)
        if not breaker.allow_execution():
            logger.error(f"工具 {tool_name} 处于熔断状态，拒绝执行")
            error = f"[circuit_breaker] 工具 {tool_name} 近期频繁失败，已被服务端临时熔断隔离，请 60 秒后再试。"
            result = f"工具执行失败: {error}"

            if self._audit:
                try:
                    await self._audit.write(
                        audit_id=exec_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        risk_level=tool_def.risk_level,
                        policy=tool_def.policy,
                        error=error,
                        status="failed",
                    )
                except Exception as audit_err:
                    logger.error(f"熔断写审计失败: {audit_err}")

            yield ToolResultEvent(result=result, tool_name=tool_name, error=error, exec_id=exec_id)
            return

        # 2. 更新状态为 executing
        if self._audit:
            try:
                await self._audit.write(
                    audit_id=exec_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    risk_level=tool_def.risk_level,
                    policy=tool_def.policy,
                    status="executing",
                )
            except Exception as e:
                logger.error(f"更新 executing 状态审计失败: {e}")

        # 执行工具，记录耗时，支持重试
        started_at = datetime.now(UTC)
        audit_id = exec_id  # 关联到已生成的 exec_id，用于状态更新与全链路追溯
        result = None
        error: str | None = None

        max_retries = 2
        retry_delay = 1.0
        retry_count = 0  # T1-4：实际重试次数（0 表示首次即成功）

        for attempt in range(max_retries + 1):
            try:
                with observe_tool(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    exec_id=exec_id,
                    session_id=session_id,
                    risk_level=tool_def.risk_level,
                ) as tool_obs:
                    try:
                        # T-AGT-22: 使用 active_executor 执行工具，显式传递 conversation_id 和 exec_id
                        result = await active_executor.execute(
                            tool_name,
                            tool_args,
                            conversation_id=session_id,
                            exec_id=exec_id,
                            case_id=case_id,
                            tool_def=tool_def,
                        )

                        if tool_obs is not None:
                            output = str(result)[:10000] if result else ""
                            tool_obs.update(output=output)

                        # 检查结果是否是超时，若超时则主动抛错重试
                        exit_code_meaning = None
                        if result:
                            if hasattr(result, "exit_code_meaning"):
                                exit_code_meaning = result.exit_code_meaning
                            elif isinstance(result, dict):
                                exit_code_meaning = result.get("exit_code_meaning")

                        if ToolRetryPolicy.is_retriable(exit_code_meaning, None):
                            raise Exception(f"工具执行返回超时状态: {exit_code_meaning}")

                        error = None
                        break
                    except Exception as e:
                        if tool_obs is not None:
                            tool_obs.update(status_message=str(e)[:1000])
                        raise
            except Exception as e:
                error = str(e)
                if attempt < max_retries:
                    # 检查异常或返回值是否可重试
                    exit_code_meaning = None
                    if result:
                        if hasattr(result, "exit_code_meaning"):
                            exit_code_meaning = result.exit_code_meaning
                        elif isinstance(result, dict):
                            exit_code_meaning = result.get("exit_code_meaning")

                    if ToolRetryPolicy.is_retriable(exit_code_meaning, error):
                        logger.warning(
                            "tool_retry",
                            f"工具 {tool_name} 执行失败(可重试): {error}. 将在 {retry_delay:.1f}s 后进行第 {attempt + 1} 次重试",
                        )
                        retry_count = attempt + 1  # T1-4：进入下一次重试前累加
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 2.0
                        continue
                result = f"工具执行失败: {error}"
                break

        # 3. 更新熔断状态
        if error:
            breaker.record_failure()
        else:
            breaker.record_success()

        # 4. 最终结果落库审计
        completed_at = datetime.now(UTC)
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)

        # T4-2: 统计工具调用成功率与耗时分布 metrics
        try:
            from app.services.metrics import AGENT_TOOL_CALL_TOTAL, AGENT_TOOL_EXECUTION_DURATION

            status_str = "success" if error is None else "failed"
            AGENT_TOOL_CALL_TOTAL.labels(tool_name=tool_name, status=status_str).inc()
            AGENT_TOOL_EXECUTION_DURATION.labels(tool_name=tool_name, status=status_str).observe(
                (completed_at - started_at).total_seconds()
            )
        except Exception as met_err:
            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")

        # 审计记录
        if self._audit:
            try:
                from shared.observability.otel import get_current_trace_id

                await self._audit.write(
                    audit_id=audit_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    risk_level=tool_def.risk_level,
                    policy=tool_def.policy,
                    result=result,
                    error=error,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    trace_id=get_current_trace_id(),
                    status="failed" if error else "committed",
                    retry_count=retry_count,  # T1-4：落库重试次数
                )
            except Exception as audit_err:
                logger.error(f"审计日志写入失败: {audit_err}")

        await self._audit_tool_resource_usage(
            tool_def=tool_def,
            tool_name=tool_name,
            tool_args=tool_args,
            result=result,
            error=error,
            session_id=session_id,
            case_id=case_id,
            exec_id=exec_id,
            status="failed" if error else "success",
        )

        # T2-2: 工具执行结果写入 FactStore（形成 Evidence 闭环）
        if error is None and self._fact_store:
            try:
                import time

                from shared.models.information import FactSource, InformationPacket

                # 将工具结果封装为 InformationPacket
                result_value = result
                if hasattr(result, "stdout"):
                    # ExecResult dataclass 或类似结构
                    result_value = {
                        "stdout": result.stdout if hasattr(result, "stdout") else str(result),
                        "stderr": result.stderr if hasattr(result, "stderr") else "",
                        "exit_code": result.exit_code if hasattr(result, "exit_code") else 0,
                        "container": getattr(result, "container", None),
                        "original_command": getattr(result, "original_command", None),
                        "built_command": getattr(result, "built_command", None),
                    }
                elif isinstance(result, dict):
                    result_value = result
                else:
                    result_value = {"output": str(result)}
                packet = InformationPacket(
                    key=f"tool_exec:{tool_name}:{exec_id}",
                    value=result_value,
                    source=FactSource.TOOL_EXEC,
                    freshness_ts=time.time(),
                    confidence=1.0,  # 工具执行结果置信度最高
                    tags=[tool_name, "tool_exec"],
                )
                await self._fact_store.write(session_id, packet, fact_type="tool_exec")
                logger.debug(
                    event="tool_result_written_to_factstore",
                    tool_name=tool_name,
                    exec_id=exec_id,
                    session_id=session_id,
                )
            except Exception as fact_err:
                logger.warning(f"工具结果写入 FactStore 失败: {fact_err}")

        # T-AGT-25: 处理 sop_request_variable 的 VariableRequestResult
        if isinstance(result, VariableRequestResult) and result.needs_input:
            # 变量请求需要用户输入，yield AgentStageUpdate 和 AgentInteractiveRequest
            yield AgentStageUpdate(
                stage="tool_result",
                metadata={
                    "exec_id": exec_id,
                    "tool_name": tool_name,
                    "status": "success",
                    "result": {"needs_input": True, "variable_name": result.variable_name, "message": result.message},
                    "error": None,
                    "duration_ms": duration_ms,
                },
            )

            var_schema = result.variable_schema
            yield AgentInteractiveRequest(
                request_id=str(uuid.uuid4()),
                acp_session_id=session_id,
                kind=result.kind,  # "variable_input" or "variable_confirm"
                title=f"填写变量：{var_schema.get('display_name', result.variable_name)}",
                prompt=var_schema.get("description", f"请提供变量 {result.variable_name} 的值"),
                options=result.options or [],
                custom_input=not result.options,
                metadata={
                    "variable_name": result.variable_name,
                    "validation_pattern": var_schema.get("validation_pattern"),
                    "variable_type": var_schema.get("type", "string"),
                    "required": var_schema.get("required", True),
                    "sop_tool": "sop_request_variable",
                    "current_value": result.current_value,  # 注入当前推断的默认/建议值
                },
            )
            # 返回特殊结果，告知主循环需要等待用户响应
            # 这里不返回 ToolResultEvent，而是返回等待状态
            yield ToolResultEvent(
                result={"needs_input": True, "variable_name": result.variable_name, "message": result.message},
                tool_name=tool_name,
                error=None,
                exec_id=exec_id,
            )
            return

        res_val = result
        is_success = error is None
        if hasattr(result, "stdout") and hasattr(result, "stderr"):
            exit_code = getattr(result, "exit_code", 0)
            res_val = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": exit_code,
            }
            if exit_code != 0:
                is_success = False
        else:
            if isinstance(result, dict) and (result.get("exit_code", 0) != 0 or result.get("error")):
                is_success = False
            res_val = str(result) if result is not None else ""

        yield AgentStageUpdate(
            stage="tool_result",
            metadata={
                "exec_id": exec_id,
                "tool_name": tool_name,
                "status": "success" if is_success else "failed",
                "result": res_val,
                "error": error or (res_val.get("stderr") if isinstance(res_val, dict) else None),
                "duration_ms": duration_ms,
            },
        )

        # 返回工具执行结果给主循环（避免重复执行）
        yield ToolResultEvent(result=result, tool_name=tool_name, error=error, exec_id=exec_id)

    async def _audit_tool_resource_usage(
        self,
        *,
        tool_def: Any,
        tool_name: str,
        tool_args: dict[str, Any],
        result: Any,
        error: str | None,
        session_id: str,
        case_id: str,
        exec_id: str,
        status: str,
    ) -> None:
        """写入工具动态资源使用审计。"""
        if self._db_session_factory is None:
            return
        try:
            from shared.dynamic_resource.loader import DynamicResourceLoader
            from shared.dynamic_resource.models import UsageRecord
            from shared.observability.otel import get_current_trace_id

            async with self._db_session_factory() as session:
                snapshot = await DynamicResourceLoader(session).get_active("tool", tool_name)
                await DynamicResourceLoader(session).audit_usage(
                    snapshot,
                    UsageRecord(
                        consumer="agent-service.react_engine",
                        status=status,
                        conversation_id=session_id,
                        case_id=case_id,
                        trace_id=get_current_trace_id(),
                        exec_id=exec_id,
                        input_payload=tool_args,
                        output_payload=result,
                        error=error,
                        metadata={
                            "risk_level": getattr(tool_def, "risk_level", None),
                            "policy": getattr(tool_def, "policy", None),
                        },
                    ),
                )
                await session.commit()
        except Exception as audit_err:
            logger.warning(
                event="dynamic_resource_tool_audit_failed",
                tool_name=tool_name,
                exec_id=exec_id,
                error=str(audit_err),
            )

    async def _persist_tool_turn(
        self,
        *,
        session_id: str,
        case_id: str,
        tool_calls_msg: dict,
        tool_result_msg: dict,
        exec_id: str | None = None,
    ) -> None:
        """将 ReAct 工具调用轮次持久化到 conversation-service（fire-and-forget）。

        实现原理（第一性原理）：
          LLM 的上下文窗口是其唯一工作内存。OpenAI Function Calling 规范要求
          tool_calls（assistant 消息）和 tool_result（tool 消息）必须成对出现
          在 messages[] 中，否则 LLM 无法知道自己已执行过哪些工具，会重复调用。

          本方法等价于 LangGraph Checkpointer 的 ReAct state 持久化：
          每步工具执行后保存检查点，中断恢复时从检查点加载完整上下文。

        Args:
            session_id: 会话 ID（conversation_id）
            case_id: 工单 ID
            tool_calls_msg: OpenAI assistant message（含 tool_calls 数组）
            tool_result_msg: OpenAI tool message（含 tool_call_id + content）
            exec_id: 工具执行 ID（供审计追踪）
        """
        if not self._conversation_service_url:
            return

        import httpx

        url = f"{self._conversation_service_url.rstrip('/')}/api/conversations/{session_id}/tool-turn"
        payload = {
            "case_id": case_id,
            "tool_calls_msg": tool_calls_msg,
            "tool_result_msg": tool_result_msg,
            "exec_id": exec_id,
        }
        headers = {}
        if self._internal_token:
            headers["Authorization"] = f"Bearer {self._internal_token}"

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code not in (200, 201):
                    logger.warning(
                        event="tool_turn_persist_failed",
                        message=f"工具调用轮次持久化失败，HTTP {resp.status_code}",
                        session_id=session_id,
                        exec_id=exec_id,
                        status_code=resp.status_code,
                    )
                else:
                    logger.debug(
                        event="tool_turn_persist_ok",
                        message="工具调用轮次已持久化",
                        session_id=session_id,
                        exec_id=exec_id,
                    )
        except Exception as e:
            # 持久化失败不阻断主流程，仅记录警告
            logger.warning(
                event="tool_turn_persist_error",
                message=f"工具调用轮次持久化异常（非阻塞）: {e}",
                session_id=session_id,
                exec_id=exec_id,
            )
