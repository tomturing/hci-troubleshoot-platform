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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from opentelemetry import trace
from pydantic import BaseModel
from shared.clients import AIAssistantRegistry
from shared.observability.logger import get_logger

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
    轻量级无依赖的 JSON Schema 与参数有效性校验器
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

        # 2. 类型及正则格式校验
        for name, value in args.items():
            if name not in properties:
                # 允许传入额外的不在 schema 中的参数而不报错
                continue

            prop_def = properties[name]
            expected_type = prop_def.get("type")

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

                # IP 字段/格式的强制 IPv4 校验
                if "ip" in name.lower() or prop_def.get("format") == "ipv4":
                    ip_regex = r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
                    if not re.match(ip_regex, value):
                        return False, f"参数 '{name}' 格式错误: '{value}' 不是有效的 IPv4 地址"

            elif expected_type == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    return False, f"参数 '{name}' 类型错误: 期望 integer，实际为 {type(value).__name__}"

            elif expected_type == "number":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    return False, f"参数 '{name}' 类型错误: 期望 number，实际为 {type(value).__name__}"

            elif expected_type == "boolean":
                if not isinstance(value, bool):
                    return False, f"参数 '{name}' 类型错误: 期望 boolean，实际为 {type(value).__name__}"

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
MAX_STEPS = 15


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
    ) -> None:
        self._ai_registry = ai_registry
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._confirm_service = confirm_service
        self._audit = audit_service
        self._fact_store = fact_store
        self.schema_validation_failed = False
        self.has_write_operation = False
        self.has_verification_after_write = False

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

        # T3-3: 输出可审计推理摘要。生产环境不要求暴露完整隐藏思维链，只展示证据化摘要。
        system_prompt += (
            "\n\n【可展示推理摘要强制要求】\n"
            "在你输出最终回答或调用工具之前，你必须使用 `<reasoning>` 标签包裹一段可展示的推理摘要。"
            "该摘要只列出证据、假设、置信度和下一步行动，不要暴露冗长的隐藏思维链或无证据猜测。摘要必须包含：\n"
            "1. 已收集证据（引用实际工具输出或已知事实）；\n"
            "2. 假设支撑与反对情况；\n"
            "3. 置信度评估（高/中/低，并说明证据是否充分）；\n"
            "4. 下一步行动说明（继续采集、验证或给出结论）。\n"
            "例如：\n"
            "<reasoning>\n"
            "1. 已收集证据：已执行 get_active_alerts，返回存在存储相关告警；尚未执行磁盘 SMART 检查。\n"
            "2. 假设支撑/反对：存储告警支持磁盘或链路异常；缺少 SMART/链路计数器作为直接证据。\n"
            "3. 置信度评估：中，当前证据只能支持疑似判断。\n"
            "4. 下一步行动：调用只读诊断工具补充磁盘与链路状态。\n"
            "</reasoning>\n"
            "请确保 `<reasoning>` 标签放在你输出的最开头。"
        )

        if response_schema:
            schema_json = json.dumps(response_schema.model_json_schema(), ensure_ascii=False)
            system_prompt += (
                f"\n\n【结构化输出强制要求】\n"
                f"你必须输出符合以下 JSON Schema 的结构化 JSON：\n"
                f"{schema_json}\n"
                f"确保你的最终文本回复必须是合法的 JSON（位于 <reasoning> 之外），不要在 JSON 外包裹任何 markdown 或自然语言解释。"
            )

        # 工作消息列表（在循环中动态追加）
        work_messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *messages,
        ]

        def extract_json(text: str) -> str:
            text_clean = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
            match = re.search(r"```json\s*(.*?)\s*```", text_clean, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
            match = re.search(r"```\s*(.*?)\s*```", text_clean, re.DOTALL)
            if match:
                return match.group(1).strip()
            start = text_clean.find('{')
            end = text_clean.rfind('}')
            if start != -1 and end != -1 and end > start:
                return text_clean[start:end+1].strip()
            return text_clean.strip()

        # 工具列表（OpenAI function calling 格式）+ 动态注入工具（T-AGT-22）
        tools = self._get_tools_for_llm(extra_tools=extra_tools, sop_mode=sop_mode)

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
                )
            except Exception as exc:
                logger.error(
                    event="react_invoke_error",
                    error=str(exc),
                    step=step_count,
                    session_id=session_id,
                )
                yield AgentTextChunk(content=f"[错误] LLM 调用失败：{exc}")
                return

            # ── 终止条件：LLM 给出文字回复 ─────────────────────────────────
            if invoke_result.content is not None:
                # T3-5: 校验优先强绑定约束检测
                if self.has_write_operation and not self.has_verification_after_write:
                    closure_keywords = ["已恢复", "已解决", "修复", "成功", "搞定", "完成", "正常", "恢复正常", "排障结束", "closure", "resolved", "fixed", "success"]
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
                            )
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
                    llm_text=invoke_result.content,
                    executed_tools=executed_tool_names,
                    tool_outputs=tool_results_list
                )

                if detection_report.get("has_hallucination"):
                    logger.warning("hallucination_detected_before_report", "最终报告生成前检测到幻觉，尝试重新生成一次 (Re-run)...")
                    try:
                        temp_messages = work_messages + [
                            {"role": "assistant", "content": invoke_result.content},
                            {"role": "system", "content": "【反幻觉自我检查指令】你的上一次回答中包含未实际执行的工具引用，或者未在工具输出中找到数据来源的数值/百分比。请进行一步自我检查，修正这些幻觉，仅引用实际执行过的工具及对应的结果。请重新输出你的回答。"}
                        ]
                        new_invoke_result = await ai_client.invoke(
                            messages=temp_messages,
                            tools=tools,
                            user_id=session_id,
                            case_id=case_id,
                        )
                        if new_invoke_result.content is not None:
                            logger.info("rerun_success", "Re-run 重新生成成功")
                            invoke_result = new_invoke_result
                    except Exception as re_exc:
                        logger.warning("rerun_failed", f"Re-run 失败: {re_exc}")

                # T3-2: 校验 Schema
                if response_schema:
                    cleaned_json = extract_json(invoke_result.content)
                    try:
                        parsed = response_schema.model_validate_json(cleaned_json)
                        logger.info("schema_validation_success", f"结构化输出校验成功: schema={response_schema.__name__}")
                        try:
                            from app.services.metrics import AGENT_SCHEMA_VALIDATION_TOTAL
                            AGENT_SCHEMA_VALIDATION_TOTAL.labels(schema_name=response_schema.__name__, status="success").inc()
                        except Exception as met_err:
                            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")
                        if response_schema.__name__ == "ClaimVerification" and self._fact_store:
                            await self._fact_store.write_claim_verification(session_id, parsed)
                    except Exception as e:
                        logger.warning("schema_validation_failed", f"结构化输出校验失败: schema={response_schema.__name__}, error={e}, raw={invoke_result.content}")
                        self.schema_validation_failed = True
                        try:
                            from app.services.metrics import AGENT_SCHEMA_VALIDATION_TOTAL
                            AGENT_SCHEMA_VALIDATION_TOTAL.labels(schema_name=response_schema.__name__, status="failed").inc()
                        except Exception as met_err:
                            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")

                # 流式输出最终文字回复
                full_stream_text = ""
                async for chunk in ai_client.chat_completion_stream(
                    messages=work_messages,
                    user_id=session_id,
                    case_id=case_id,
                ):
                    if chunk:
                        full_stream_text += chunk
                        yield AgentTextChunk(content=chunk)

                # 流式输出后校验 (如果是重新生成文本的话)
                if response_schema and not self.schema_validation_failed:
                    cleaned_json = extract_json(full_stream_text)
                    try:
                        parsed = response_schema.model_validate_json(cleaned_json)
                        try:
                            from app.services.metrics import AGENT_SCHEMA_VALIDATION_TOTAL
                            AGENT_SCHEMA_VALIDATION_TOTAL.labels(schema_name=response_schema.__name__, status="success").inc()
                        except Exception as met_err:
                            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")
                        if response_schema.__name__ == "ClaimVerification" and self._fact_store:
                            await self._fact_store.write_claim_verification(session_id, parsed)
                    except Exception as e:
                        logger.warning("stream_schema_validation_failed", f"流式结构化输出校验失败: {e}, raw={full_stream_text}")
                        self.schema_validation_failed = True
                        try:
                            from app.services.metrics import AGENT_SCHEMA_VALIDATION_TOTAL
                            AGENT_SCHEMA_VALIDATION_TOTAL.labels(schema_name=response_schema.__name__, status="failed").inc()
                        except Exception as met_err:
                            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")

                # T3-4: 运行轻量级幻觉检测器
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

                final_text = full_stream_text or invoke_result.content or ""
                detection_report = detector.detect(
                    llm_text=final_text,
                    executed_tools=executed_tool_names,
                    tool_outputs=tool_results_list
                )

                if detection_report.get("has_hallucination"):
                    reasons = detection_report.get("reasons", [])
                    warning_msg = f"\n\n*(注：本回复中部分内容存在高风险幻觉（如：{', '.join(reasons)}），已标注为\"待验证\"，请工程师注意确认)*"
                    yield AgentTextChunk(content=warning_msg)
                    try:
                        from app.services.metrics import AGENT_HALLUCINATION_DETECTED_TOTAL
                        for htype, hkey in [("phantom_tools", "phantom_tool"), ("overconfident_claims", "overconfident"), ("ungrounded_numbers", "ungrounded_number")]:
                            if detection_report.get(htype):
                                AGENT_HALLUCINATION_DETECTED_TOTAL.labels(hallucination_type=hkey).inc()
                    except Exception as met_err:
                        logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")

                return

            # ── 工具调用轮次 ──────────────────────────────────────────────────
            if not invoke_result.tool_calls:
                # invoke() 返回空（不应发生），安全退出
                logger.warning(
                    event="react_empty_result",
                    step=step_count,
                    session_id=session_id,
                )
                yield AgentTextChunk(content="诊断推理已完成。")
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
                    from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY
                    temp_tool_def = TOOL_REGISTRY.get(tc.name)
                temp_risk = temp_tool_def.risk_level if temp_tool_def else 1
                if temp_risk >= 2:
                    self.has_write_operation = True
                    self.has_verification_after_write = False
                elif self.has_write_operation and tc.name not in ("get_sop_node", "sop_advance", "sop_request_variable"):
                    self.has_verification_after_write = True

                # require_all_confirm 覆盖：将只读工具也升级为需确认
                tool_result = None
                tool_exec_id = tc.id
                async for event in self._execute_tool_call(
                    tool_call=tool_call_dict,
                    session_id=session_id,
                    step=step_count,
                    require_all_confirm=require_all_confirm,
                    tool_executor=active_tool_executor,  # T-AGT-22: 传入执行器
                    execution_mode=execution_mode,       # T1-3: 传入执行模式
                ):
                    # 捕获工具执行结果
                    if isinstance(event, ToolResultEvent):
                        tool_result = event.result
                        if event.exec_id:
                            tool_exec_id = event.exec_id
                    # AgentTextChunk 需要传递给外层（如"操作已取消"、"确认服务暂不可用"）
                    elif isinstance(event, AgentTextChunk):
                        yield event
                        # 工具执行被取消或失败时，终止循环
                        if "取消" in event.content or "中止" in event.content or "失败" in event.content:
                            return
                    else:
                        yield event

                # 将工具结果以 ToolResultEnvelope 的结构化形式追加到消息历史 (T0-1)
                envelope = ToolResultEnvelope.from_raw_result(
                    tool_name=tc.name,
                    exec_id=tool_exec_id,
                    result=tool_result,
                )
                work_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": envelope.to_llm_message(),
                    }
                )


        # 超出步数限制
        yield AgentTextChunk(content="⚠️ 诊断步骤已达上限，请联系人工支持。")

    def _get_tools_for_llm(self, extra_tools: list[dict] | None = None, sop_mode: bool = False) -> list[dict]:
        """返回 OpenAI function calling 格式 of 工具列表（排除高危工具）。

        Args:
            extra_tools: 动态注入的工具列表（T-AGT-22），追加到默认工具列表末尾
            sop_mode: 是否是 SOP 模式，如果是 SOP 模式则包含 SOP 导航工具

        Returns:
            工具列表（OpenAI function calling 格式）
        """
        from app.adapters.agents.htp.tool_registry import get_tools_for_llm

        base_tools = get_tools_for_llm(include_sop=sop_mode)
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
        execution_mode: str = "safe-only",           # T1-3: 执行模式（off/safe-only/aggressive）
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
        from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY
        from app.tools.acli.classifier import classify_acli, classify_bash, risk_to_policy

        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("args", {})
        exec_id = str(uuid.uuid4())

        # T3-2: 降级拦截 - 如果前序推理格式校验失败，禁止执行高风险写操作工具
        if getattr(self, "schema_validation_failed", False):
            tool_def = TOOL_REGISTRY.get(tool_name)
            risk = tool_def.risk_level if tool_def else 1
            if risk >= 2:
                logger.warning("schema_validation_block_write", f"降级拦截: Schema 校验失败，禁止执行高风险写操作工具: {tool_name}")
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

                temp_tool_def = TOOL_REGISTRY.get(tool_name)
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

        tool_def = TOOL_REGISTRY.get(tool_name)
        if not tool_def:
            # T-AGT-22: SOP 工具在 TOOL_REGISTRY 中已定义，此检查覆盖所有已注册工具
            yield AgentTextChunk(content=f"未知工具: {tool_name}")
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
            # 返回校验失败的错误结果给 LLM
            yield ToolResultEvent(
                result=f"[error] 参数校验失败：{err_msg}。请修正参数后重新尝试调用该工具。",
                tool_name=tool_name,
                error=err_msg,
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
                yield AgentTextChunk(content=f"[blocked] 命令 {command!r} 属于高危操作（risk=3），已拒绝执行。")
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
            yield AgentTextChunk(content=f"工具 {tool_name} 风险等级过高，已阻止执行")
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
            # 由上游 _stream_with_tools 负责将其追加到 work_messages 并回填给 LLM。
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
                with tracer.start_as_current_span("tool.execute") as span:
                    span.set_attribute("tool.name", tool_name)
                    span.set_attribute("tool.risk_level", tool_def.risk_level)
                    span.set_attribute("session_id", session_id)
                    span.set_attribute("attempt", attempt)
                    try:
                        # T-AGT-22: 使用 active_executor 执行工具，显式传递 conversation_id 和 exec_id
                        result = await active_executor.execute(
                            tool_name, tool_args, conversation_id=session_id, exec_id=exec_id
                        )

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
                        span.record_exception(e)
                        span.set_status(trace.StatusCode.ERROR, str(e))
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
                            f"工具 {tool_name} 执行失败(可重试): {error}. 将在 {retry_delay:.1f}s 后进行第 {attempt + 1} 次重试"
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

        # T4-2: 统计工具调用成功率 metrics
        try:
            from app.services.metrics import AGENT_TOOL_CALL_TOTAL
            AGENT_TOOL_CALL_TOTAL.labels(
                tool_name=tool_name,
                status="success" if error is None else "failed"
            ).inc()
        except Exception as met_err:
            logger.warning("metrics_record_failed", f"记录 metrics 失败: {met_err}")

        # 4. 最终结果落库审计
        completed_at = datetime.now(UTC)
        duration_ms = int((completed_at - started_at).total_seconds() * 1000)

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

        # 广播 tool_result 结果事件
        yield AgentStageUpdate(
            stage="tool_result",
            metadata={
                "exec_id": exec_id,
                "tool_name": tool_name,
                "status": "success" if error is None else "failed",
                "result": str(result),
                "error": error,
                "duration_ms": duration_ms,
            },
        )

        # 返回工具执行结果给主循环（避免重复执行）
        yield ToolResultEvent(result=result, tool_name=tool_name, error=error, exec_id=exec_id)
