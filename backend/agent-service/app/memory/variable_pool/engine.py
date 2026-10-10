"""
变量池 — 策略决策内核（Control Plane）

负责 JIT 变量获取流程的决策与协调，本身无状态，通过外部注入的执行器（tool_executor）与客户端执行具体动作。

--------------------------------------------------------------------------------
🎯 理想微内核演进蓝图 & 方案一（逻辑外观层软解耦）设计规范：
本模块遵循“控制与执行解耦的微内核架构（Microkernel Architecture）”：
- 物理层保持高稳定性：保留原有的 tools/acli/ 和 tools/sop/ 等技术分类包。
- 逻辑外观层（Facade）实现 100% 理想微内核语义对齐：
  本引擎在决策和执行获取策略时，只涉及 app.tools 暴露的 SystemTools (对应 tool_call)
  和 InteractiveTools (对应 user_confirm/user_input) 逻辑命名空间，
  彻底与具体的物理文件名/子目录名脱耦。
- 后续迭代：所有新工具与交互组件应优先在 `app.tools` Facade 中映射为逻辑工具组，
  使 JIT 引擎能够平滑地演进到 Option 2 物理分拆的目标状态。
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from shared.clients import KBClient
from shared.observability.logger import get_logger
from shared.utils.acquisition_strategy import (
    STRATEGY_DERIVED,
    STRATEGY_ENV_INJECTION,
    STRATEGY_JSON_EXTRACT,
    STRATEGY_SKILL_CALL,
    STRATEGY_SOP_DEFAULT,
    STRATEGY_TOOL_CALL,
    STRATEGY_USER_CONFIRM,
    parse_strategy,
)

from app.memory.variable_pool.pool import VariableRequestResult
from app.skills.errors import (
    SkillError,
    SkillLLMError,
    SkillNotFoundError,
    SkillOutputUnavailableError,
)

logger = get_logger("memory.variable-pool")

# ADR-2：占位符统一为 {{VAR}} 全大写（支持点分路径如 {{NODE.IP}}）
# SOP 参数模板使用单花括号 {var}，同时兼容 {{VAR}} 写法（大小写敏感查表）
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_.]*)\}\}|\{([A-Za-z][A-Za-z0-9_.]*)\}")
_EXACT_TEMPLATE_PLACEHOLDER_RE = re.compile(r"^\{\{([A-Za-z][A-Za-z0-9_.]*)\}\}|\{([A-Za-z][A-Za-z0-9_.]*)\}$")


# 设备缺失（盘符为空/内核无此设备）判别标记：区别于“命令/工具缺失”，
# 用于 Q2026100812343 场景中已摘除磁盘的准确归类（V-017 §4 device_absent）。
_DEVICE_ABSENT_MARKERS: tuple[str, ...] = (
    "no such device",
    "cannot open /dev/",
    "unable to open /dev/",
    "device does not exist",
    "not a valid device",
    "unable to detect device type",
    "no such disk",
    "设备不存在",
    "找不到设备",
    "磁盘不存在",
    "盘符不存在",
)


def _is_device_absent(text: str) -> bool:
    """判断工具输出/异常文本是否表征“目标设备不存在/已摘除”。"""
    lowered = text.lower()
    return any(marker in lowered for marker in _DEVICE_ABSENT_MARKERS)


def _classify_acquisition_error(exc: Exception) -> str:
    """把采集异常归一为可判别的 error_type，禁止大而全、模糊归一的分类（V-017）。"""
    text = str(exc).lower()
    if "conversation_id" in text or "缺少 conversation" in text or "missing 1 required" in text:
        return "contract_error"
    if _is_device_absent(text):
        return "device_absent"
    if "oniguruma" in text or "command not found" in text or "not available" in text:
        return "tool_capability_missing"
    if "timeout" in text or "timed out" in text or "超时" in text:
        return "timeout"
    if "unreachable" in text or "connection" in text or "refused" in text or "不可达" in text or "拒绝" in text:
        return "node_unreachable"
    if "无对应执行器" in text or "executor_unavailable" in text or ("executor" in text and "未初始化" in text):
        return "executor_unavailable"
    if isinstance(exc, (TypeError, ValueError)):
        return "contract_error"
    return "tool_error"


def _classify_tool_result_error(tool_result: Any) -> str:
    """工具已执行但未取到值时的 error_type 归类。"""
    if isinstance(tool_result, dict):
        err_text = str(tool_result.get("error", "")).lower()
        out_text = str(tool_result.get("stdout", tool_result.get("output", "")) or "").lower()
    else:
        err_text = str(getattr(tool_result, "stderr", "") or "").lower()
        out_text = str(getattr(tool_result, "stdout", "") or "").lower()
    if "conversation_id" in err_text or "缺少 conversation" in err_text:
        return "contract_error"
    # 设备缺失判别早于工具能力/退出码，准确区分“盘没了”与“命令没跑成”（§4 device_absent）
    if _is_device_absent(err_text) or _is_device_absent(out_text):
        return "device_absent"
    if "oniguruma" in err_text or "command not found" in err_text or "not available" in err_text:
        return "tool_capability_missing"
    if bool(getattr(tool_result, "timed_out", False)) or bool(getattr(tool_result, "timeout", False)):
        return "timeout"
    if "timeout" in err_text or "timed out" in err_text:
        return "timeout"
    if "unreachable" in err_text or "connection" in err_text or "refused" in err_text:
        return "node_unreachable"
    exit_code = getattr(tool_result, "exit_code", None)
    if isinstance(exit_code, int) and exit_code != 0:
        return "tool_error"
    return "empty_value"


def _read_path(payload: Any, path: str) -> Any:
    """按点分路径从 dict/list/dataclass 对象中读取值。"""
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if 0 <= idx < len(current) else None
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            return None
        if current is None:
            return None
    return current


def _extract_value(payload: Any, variable_name: str, output_path: str | None = None) -> Any:
    """从 Tool/Skill 输出中提取变量值。"""
    if payload is None:
        return None

    if output_path:
        return _read_path(payload, output_path)

    # 通用命令执行器返回 ExecResult/dataclass 时，变量值默认绑定 stdout。
    if hasattr(payload, "stdout"):
        return payload.stdout

    if not isinstance(payload, dict):
        return payload

    if variable_name in payload:
        return payload.get(variable_name)
    values = payload.get("values")
    if isinstance(values, dict) and variable_name in values:
        return values.get(variable_name)
    for key in ("value", "result", "output"):
        if key in payload:
            return payload.get(key)
    return None


def _unwrap_context_variables(context_variables: dict[str, Any]) -> dict[str, Any]:
    """把变量池记录解包为 Skill/Tool 可直接消费的上下文字典。"""
    unwrapped: dict[str, Any] = {}
    for name, payload in context_variables.items():
        if isinstance(payload, dict) and "value" in payload:
            unwrapped[name] = payload.get("value")
        else:
            unwrapped[name] = payload
    return unwrapped


def _render_args_template(template: Any, context_variables: dict[str, Any]) -> Any:
    """渲染 Tool 参数模板，支持 dict/list/string 递归替换 `{var}` 占位符。"""
    context = _unwrap_context_variables(context_variables)

    def resolve(path: str) -> Any:
        value = _read_path(context, path)
        if value is None:
            raise ValueError(f"参数模板引用的变量 {path} 尚未就绪")
        return value

    def render(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: render(item) for key, item in value.items()}
        if isinstance(value, list):
            return [render(item) for item in value]
        if not isinstance(value, str):
            return value

        exact_match = _EXACT_TEMPLATE_PLACEHOLDER_RE.match(value)
        if exact_match:
            return resolve(exact_match.group(1) or exact_match.group(2))

        def replace(match: re.Match[str]) -> str:
            return str(resolve(match.group(1) or match.group(2)))

        return _TEMPLATE_PLACEHOLDER_RE.sub(replace, value)

    return render(template)


def _split_args(text: str) -> list[str]:
    """拆分函数参数，兼容简单引号和嵌套括号。"""
    args: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
            current.append(char)
            continue
        if char == ")":
            depth -= 1
            current.append(char)
            continue
        if char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        args.append("".join(current).strip())
    return args


def _split_ternary(expression: str) -> tuple[str, str, str] | None:
    """拆分 `condition ? true_expr : false_expr`，不支持任意代码执行。"""
    quote: str | None = None
    depth = 0
    question_idx: int | None = None
    for idx, char in enumerate(expression):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth -= 1
            continue
        if char == "?" and depth == 0:
            question_idx = idx
            break
    if question_idx is None:
        return None

    quote = None
    depth = 0
    for idx in range(question_idx + 1, len(expression)):
        char = expression[idx]
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char == "(":
            depth += 1
            continue
        if char == ")":
            depth -= 1
            continue
        if char == ":" and depth == 0:
            return (
                expression[:question_idx].strip(),
                expression[question_idx + 1 : idx].strip(),
                expression[idx + 1 :].strip(),
            )
    return None


def _evaluate_derived_expression(expression: str, context_variables: dict[str, Any]) -> Any:
    """执行派生变量白名单表达式。"""
    context = _unwrap_context_variables(context_variables)

    def eval_atom(expr: str) -> Any:
        expr = expr.strip()
        ternary = _split_ternary(expr)
        if ternary:
            condition_expr, truthy_expr, falsy_expr = ternary
            return eval_atom(truthy_expr if bool(eval_atom(condition_expr)) else falsy_expr)

        lowered = expr.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered in ("unknown", "null", "none"):
            return None
        if (expr.startswith("'") and expr.endswith("'")) or (expr.startswith('"') and expr.endswith('"')):
            return expr[1:-1]
        if re.fullmatch(r"-?\d+", expr):
            return int(expr)
        if re.fullmatch(r"-?\d+\.\d+", expr):
            return float(expr)

        function_match = re.fullmatch(r"([a-z_][a-z0-9_]*)\((.*)\)", expr)
        if function_match:
            function_name = function_match.group(1)
            args = [eval_atom(arg) for arg in _split_args(function_match.group(2))]
            if function_name == "contains" and len(args) == 2:
                return str(args[1]) in str(args[0] or "")
            if function_name == "equals" and len(args) == 2:
                return args[0] == args[1]
            if function_name == "starts_with" and len(args) == 2:
                return str(args[0] or "").startswith(str(args[1]))
            if function_name == "ends_with" and len(args) == 2:
                return str(args[0] or "").endswith(str(args[1]))
            if function_name == "not" and len(args) == 1:
                return not bool(args[0])
            if function_name == "split" and len(args) == 2:
                return str(args[0] or "").split(str(args[1]))
            if function_name == "join" and len(args) == 2:
                return str(args[1]).join(args[0] if isinstance(args[0], list) else [str(args[0])])
            raise ValueError(f"不支持的 derived 函数或参数数量: {function_name}")

        value = _read_path(context, expr)
        if value is None:
            raise ValueError(f"derived 表达式引用的变量 {expr} 尚未就绪")
        return value

    return eval_atom(expression)


def _find_var_def(name: str, variable_schema: list[dict[str, Any]]) -> dict[str, Any] | None:
    """在 variable_schema 列表中查找变量定义。"""
    for v in variable_schema:
        if isinstance(v, dict) and v.get("name") == name:
            return v
    return None


def _find_similar_variables(name: str, variable_schema: list[dict[str, Any]], limit: int = 5) -> list[str]:
    """根据 LLM 编造的变量名查找 schema 中相似的已声明变量。

    相似度 = 变量名交集字符数 / 并集字符数（Jaccard 字符级）。
    """
    if not name or not variable_schema:
        return []
    name_set = set(name.lower())
    scored: list[tuple[float, str]] = []
    for v in variable_schema:
        if not isinstance(v, dict):
            continue
        declared = v.get("name", "")
        if not declared:
            continue
        declared_set = set(declared.lower())
        intersection = len(name_set & declared_set)
        union = len(name_set | declared_set)
        if union == 0:
            continue
        similarity = intersection / union
        if similarity > 0.2:
            scored.append((similarity, declared))
    scored.sort(key=lambda x: -x[0])
    return [name for _, name in scored[:limit]]


async def _persist_variable(
    client: Any | None,
    conversation_id: str,
    variable_name: str,
    value: Any,
) -> None:
    """JIT 获取到变量值后写回 conversation-service 的 context_variables。

    这样后续依赖链上的变量可直接从池中读取，避免重复获取或 LLM 手动回溯。
    """
    if client is None or not hasattr(client, "set_variable"):
        return
    try:
        str_value = str(value) if not isinstance(value, str) else value
        # 截断过长的值（如 SMART 原始输出），避免 HTTP 请求体过大
        if len(str_value) > 50000:
            str_value = str_value[:50000] + "\n...(truncated)"
        await client.set_variable(
            uuid.UUID(conversation_id),
            variable_name,
            str_value,
            source="jit_auto_acquire",
        )
    except Exception:
        pass  # 写回失败不影响主流程（变量值已返回给 LLM）


async def sop_request_variable(
    variable_name: str,
    reason: str | None = None,
    *,
    conversation_id: str,
    sop_document_id: int,
    kb_client: KBClient,
    conversation_sop_client: Any | None = None,  # ConversationSopClient，避免循环导入用 Any
    tool_executor: Any | None = None,  # DC-02: 用于 strategy="tool_call" 自动调用工具获取变量值
    skill_runner: Any | None = None,  # DynamicSkillRunner，避免循环导入用 Any
) -> VariableRequestResult | dict[str, Any]:
    """请求获取 SOP 变量值（JIT 懒加载，T-AGT-25）。

    此函数是变量池的核心入口，实现工作记忆层的 JIT 变量获取。
    LLM 通过 tool_call 调用此工具，引擎根据 acquisition_strategy 决定获取路径。

    流程：
      1. 检查 context_variables 中是否已有值（缓存命中直接返回）
      2. 获取 SOP 文档的 variable_schema，找到变量定义
      3. 根据 acquisition_strategy 决定获取方式：
         - sop_default  : 读取 variable_schema.default_value，立即返回，无 I/O
         - tool_call   : 调用指定工具自动获取值（DC-02），失败降级为 user_input
         - user_confirm: 调用工具获取候选列表，展示给用户确认
         - user_input  : 返回 VariableRequestResult(needs_input=True) 阻塞等待
         - env_injection: 报错（此类变量应在初始化阶段注入，不走 JIT）

    Args:
        variable_name: 变量名（如 vm_name、node_ip）
        reason: 为什么需要此变量（用于向用户解释）
        conversation_id: 会话 ID（由上下文注入）
        sop_document_id: SOP 文档 ID（由上下文注入）
        kb_client: KB 服务客户端（用于获取 variable_schema）
        conversation_sop_client: Conversation SOP API 客户端（用于获取执行状态）
        tool_executor: 工具执行器（用于 tool_call / user_confirm 策略）

    Returns:
        VariableRequestResult(needs_input=True): 需要用户输入，ReactEngine 应阻塞等待
        dict(ok=True, value=...)               : 已有值或已获取到值，直接返回给 LLM
        dict(error="...")                       : 错误信息
    """
    logger.info(
        event="sop_request_variable_start",
        conversation_id=conversation_id,
        sop_document_id=sop_document_id,
        variable_name=variable_name,
        reason=reason,
    )

    # 1. 获取 SOP 执行状态（检查 context_variables）
    if conversation_sop_client is None:
        return {"error": "ConversationSopClient 未注入，无法获取执行状态"}

    execution = await conversation_sop_client.get_execution(uuid.UUID(conversation_id))
    if execution is None:
        return {"error": "SOP 执行实例不存在"}

    context_variables = execution.get("context_variables", {})
    pending_variable = execution.get("pending_variable_name")

    # 缓存命中：已有值直接返回
    if variable_name in context_variables:
        existing_value = context_variables[variable_name]
        value = existing_value.get("value") if isinstance(existing_value, dict) else existing_value

        if value is not None and value != "":
            logger.info(
                event="sop_request_variable_cached",
                conversation_id=conversation_id,
                variable_name=variable_name,
                cached_value=value,
            )
            return {
                "ok": True,
                "value": value,
                "source": "cached",
                "message": f"变量 {variable_name} 已有值：{value}",
            }

    # 防止并发变量请求（同一时刻只允许一个变量等待用户输入）
    if pending_variable and pending_variable != variable_name:
        return {
            "error": f"已有变量 {pending_variable} 正在等待用户输入，请先完成该变量填写后再请求 {variable_name}",
        }

    # 2. 获取 SOP 文档的 variable_schema
    sop_doc = await kb_client.get_sop_document(sop_document_id)
    if sop_doc is None:
        return {"error": f"SOP 文档 {sop_document_id} 不存在"}

    variable_schema_list = sop_doc.get("variable_schema", [])
    if not variable_schema_list:
        # variable_schema 未定义，允许 LLM 自行处理，降级为自由输入
        logger.warning(
            event="sop_request_variable_schema_missing",
            sop_document_id=sop_document_id,
            variable_name=variable_name,
        )
        return VariableRequestResult(
            needs_input=True,
            variable_name=variable_name,
            variable_schema={
                "name": variable_name,
                "display_name": variable_name,
                "description": reason or f"请提供变量 {variable_name} 的值",
                "type": "string",
                "required": True,
            },
            message=f"变量 {variable_name} 需要用户提供值",
            kind="variable_input",
        )

    # 查找变量定义
    var_def = None
    for v in variable_schema_list:
        if v.get("name") == variable_name:
            var_def = v
            break

    if var_def is None:
        # 变量未在 schema 中定义 — 搜索相似变量名提示 LLM 使用正确名称
        suggestions = _find_similar_variables(variable_name, variable_schema_list)
        logger.warning(
            event="sop_request_variable_not_defined",
            sop_document_id=sop_document_id,
            variable_name=variable_name,
            suggestions=suggestions,
        )
        if suggestions:
            # 有相似变量时返回结构化错误而非用户输入，让 LLM 自行修正
            return {
                "error": "sop_variable_not_defined",
                "message": (
                    f"变量 '{variable_name}' 未在 SOP Schema 中定义。"
                    f"你可能想请求: {', '.join(suggestions[:5])}。"
                    f"请使用 sop_request_variable 请求上面列出的正确变量名。"
                ),
                "variable_name": variable_name,
                "suggested_variables": suggestions[:5],
            }
        # 无相似变量时才降级为自由输入
        return VariableRequestResult(
            needs_input=True,
            variable_name=variable_name,
            variable_schema={
                "name": variable_name,
                "display_name": variable_name,
                "description": reason or f"请提供变量 {variable_name} 的值",
                "type": "string",
                "required": True,
            },
            message=f"变量 {variable_name} 未在 SOP Schema 中定义，需要用户提供值",
            kind="variable_input",
        )

    # 辅助：调用 interrupt API 并返回 VariableRequestResult（阻塞等待用户输入）
    async def _request_user_input(
        var_schema: dict,
        kind: str = "variable_input",
        options: list[dict] | None = None,
        msg: str = "",
    ) -> VariableRequestResult:
        """标记执行中断，等待用户提供变量值"""
        if var_schema.get("type") == "boolean":
            kind = "variable_confirm"
            if not options:
                options = [
                    {"optionId": "true", "name": "是 (true)"},
                    {"optionId": "false", "name": "否 (false)"},
                ]

        if conversation_sop_client:
            try:
                await conversation_sop_client.interrupt(
                    conversation_id=uuid.UUID(conversation_id),
                    pending_variable_name=variable_name,
                )
                logger.info(
                    event="sop_request_variable_interrupt_set",
                    conversation_id=conversation_id,
                    variable_name=variable_name,
                )
            except Exception as exc:
                logger.warning(
                    event="sop_request_variable_interrupt_failed",
                    conversation_id=conversation_id,
                    variable_name=variable_name,
                    error=str(exc),
                )
        return VariableRequestResult(
            needs_input=True,
            variable_name=variable_name,
            variable_schema=var_schema,
            message=msg or f"变量 {variable_name} 需要用户提供值",
            kind=kind,
            options=options or [],
        )

    # 3. 根据 acquisition_strategy 决定获取方式
    # 使用公共解析模块，支持全名/简写/冒号参数三种格式（如 "skill:hci-alert-parsing"）
    _parsed = parse_strategy(var_def.get("acquisition_strategy"))
    strategy = _parsed.strategy
    # 冒号参数（如 skill:hci-alert-parsing 中的 hci-alert-parsing）优先于独立 acquisition_tool 字段
    acquisition_tool = _parsed.parameter or var_def.get("acquisition_tool")
    output_path = var_def.get("output_path")
    depends_on = var_def.get("depends_on") or []
    if isinstance(depends_on, str):
        depends_on = [item.strip() for item in depends_on.split(",") if item.strip()]

    logger.info(
        event="sop_request_variable_strategy",
        variable_name=variable_name,
        strategy=strategy,
        raw_strategy=var_def.get("acquisition_strategy"),
        acquisition_tool=acquisition_tool,
        depends_on=depends_on,
    )

    # 递归解析依赖链：缺失的前置变量自动触发 JIT 获取，避免 LLM 手动逐步回溯
    _resolve_stack = getattr(sop_request_variable, "_resolve_stack", None)
    if _resolve_stack is None:
        _resolve_stack = set()
        sop_request_variable._resolve_stack = _resolve_stack  # type: ignore[attr-defined]

    if variable_name in _resolve_stack:
        return {
            "error": "sop_variable_circular_dependency",
            "message": f"变量 {variable_name} 存在循环依赖，解析栈: {list(_resolve_stack)}",
            "variable_name": variable_name,
        }

    missing_deps = []
    for dep in depends_on:
        dep_value = context_variables.get(dep)
        dep_payload = dep_value.get("value") if isinstance(dep_value, dict) else dep_value
        if dep_payload is None or dep_payload == "":
            missing_deps.append(dep)

    if missing_deps:
        # 仅自动解析在 variable_schema 中有声明的依赖；未声明的仍返回 missing 错误
        resolvable_deps = [d for d in missing_deps if _find_var_def(d, variable_schema_list)]
        unresolvable_deps = [d for d in missing_deps if not _find_var_def(d, variable_schema_list)]
        if unresolvable_deps and not resolvable_deps:
            return {
                "error": "sop_variable_dependency_missing",
                "message": f"变量 {variable_name} 依赖 {', '.join(missing_deps)}，请先获取依赖变量",
                "variable_name": variable_name,
                "missing_dependencies": missing_deps,
                "next_tool_call": {
                    "tool_name": "sop_request_variable",
                    "args": {
                        "variable_name": missing_deps[0],
                        "reason": f"变量 {variable_name} 的前置依赖",
                    },
                },
            }

        _resolve_stack.add(variable_name)
        try:
            for dep in resolvable_deps:
                logger.info(
                    event="sop_request_variable_resolve_dependency",
                    variable_name=variable_name,
                    dependency=dep,
                )
                dep_result = await sop_request_variable(
                    variable_name=dep,
                    reason=f"变量 {variable_name} 的前置依赖",
                    conversation_id=conversation_id,
                    sop_document_id=sop_document_id,
                    kb_client=kb_client,
                    conversation_sop_client=conversation_sop_client,
                    tool_executor=tool_executor,
                    skill_runner=skill_runner,
                )
                if isinstance(dep_result, VariableRequestResult):
                    # 依赖需要用户输入 → 向上传播
                    return dep_result
                if isinstance(dep_result, dict) and "error" in dep_result:
                    # 依赖获取失败 → 向上传播错误
                    return {
                        "error": "sop_variable_dependency_failed",
                        "message": (
                            f"无法获取变量 {variable_name} 的前置依赖 {dep}: "
                            f"{dep_result.get('message', dep_result.get('error', ''))}"
                        ),
                        "variable_name": variable_name,
                        "failed_dependency": dep,
                        "dependency_error": dep_result,
                    }
                # 依赖获取成功，刷新 context_variables
                if conversation_sop_client is not None:
                    execution = await conversation_sop_client.get_execution(uuid.UUID(conversation_id))
                    context_variables = (execution or {}).get("context_variables", {}) or {}
        finally:
            _resolve_stack.discard(variable_name)

    if strategy == STRATEGY_ENV_INJECTION:
        # env_injection 类变量（含 env:xxx 冒号格式）应在 SOP 初始化阶段批量注入，不走 JIT
        logger.error(
            event="sop_request_variable_env_injection_missing",
            message=f"变量 {variable_name} 策略为 {var_def.get('acquisition_strategy')} 但在 context_variables 中未找到值",
            variable_name=variable_name,
            conversation_id=conversation_id,
        )
        # 失败即接管：环境上下文未注入即视为采集无法完成，一律转人工补值，
        # 绝不把泛化 error 交回模型触发手工兜底（修复 B「一律转人工弹框」）。
        return await _request_user_input(
            var_schema=var_def,
            kind="variable_input",
            msg=(
                f"变量 {variable_name} 声明为 {var_def.get('acquisition_strategy')}，"
                "但环境上下文未注入、无法自动取值。请人工提供该变量值或确认后继续。"
            ),
        )

    if strategy == STRATEGY_SOP_DEFAULT:
        # sop_default 类变量：直接读 variable_schema.default_value 或冒号参数（如 sop:NONE）
        default_val = var_def.get("default_value") or (_parsed.parameter if _parsed.parameter else None)
        if default_val is None:
            return {
                "error": (
                    f"变量 {variable_name} 的 acquisition_strategy 为 sop_default，"
                    "但 variable_schema 未定义 default_value"
                ),
            }
        value = str(default_val)
        logger.info(
            event="sop_request_variable_default_resolved",
            variable_name=variable_name,
            value=value,
        )
        return {"ok": True, "value": value, "source": "sop_default"}

    if strategy == STRATEGY_DERIVED:
        expression = var_def.get("expression") or var_def.get("derived_expression")
        if not expression:
            # 配置缺失也属于“无法自动产出确定值”，失败即接管转人工，不交回模型
            logger.error(
                event="sop_request_variable_derived_expression_missing",
                variable_name=variable_name,
                conversation_id=conversation_id,
            )
            return await _request_user_input(
                var_schema=var_def,
                kind="variable_input",
                msg=(
                    f"变量 {variable_name} 声明为 derived 但未配置 expression，无法自动派生。"
                    "请人工提供该变量值或确认后继续。"
                ),
            )
        try:
            derived_value = _evaluate_derived_expression(str(expression), context_variables)
            if derived_value is not None:
                logger.info(
                    event="sop_request_variable_derived_resolved",
                    variable_name=variable_name,
                    expression=str(expression),
                )
                return {"ok": True, "value": derived_value, "source": "derived"}
        except Exception as exc:
            logger.error(
                event="sop_request_variable_derived_failed",
                variable_name=variable_name,
                expression=str(expression),
                error=str(exc),
            )
        # 失败即接管：派生表达式无法产出确定值 → 一律转人工，不交回模型
        logger.info(
            event="sop_request_variable_escalate_to_human",
            conversation_id=conversation_id,
            variable_name=variable_name,
            error_type="derived_unresolved",
        )
        return await _request_user_input(
            var_schema=var_def,
            kind="variable_input",
            msg=(
                f"变量 {variable_name} 声明由 derived 表达式计算，但表达式无法产出确定值。"
                "请人工提供该变量值或确认后继续。"
            ),
        )

    if strategy == STRATEGY_TOOL_CALL and acquisition_tool:
        # tool_call 策略（含 tool:xxx 冒号简写）：由引擎经受控通道自动调用指定工具获取变量值（DC-02）。
        # 修复 V-017：
        #   1. 必须向执行器透传 conversation_id，否则命令永远不下发；
        #   2. 采集失败一律"失败即接管"转人工弹框，绝不把控制权交回模型即兴敲命令。
        acquired_value: Any = None
        failure_reason = ""
        error_type = "empty_value"
        if tool_executor is None:
            failure_reason = f"工具执行器未初始化（{acquisition_tool}）"
            error_type = "executor_unavailable"
            logger.error(
                event="sop_request_variable_tool_no_executor",
                variable_name=variable_name,
                acquisition_tool=acquisition_tool,
                conversation_id=conversation_id,
                error_type=error_type,
            )
        else:
            try:
                tool_args = var_def.get("acquisition_args") or var_def.get("acquisition_args_template") or {}
                tool_args = _render_args_template(tool_args, context_variables)
                # 透传 conversation_id（CompositeToolExecutor / BridgeRelayExecutor 均需此关键字参数）
                tool_result = await tool_executor.execute(
                    acquisition_tool, tool_args, conversation_id=conversation_id
                )
                acquired_value = _extract_value(tool_result, variable_name, output_path)
            except (TypeError, ValueError) as exc:
                # 参数契约错误（如缺 conversation_id）、模板渲染缺前置变量等
                failure_reason = str(exc)
                error_type = _classify_acquisition_error(exc)
                logger.error(
                    event="sop_request_variable_tool_failed",
                    variable_name=variable_name,
                    acquisition_tool=acquisition_tool,
                    conversation_id=conversation_id,
                    error_type=error_type,
                    error=str(exc),
                )
            except Exception as exc:
                failure_reason = str(exc)
                error_type = _classify_acquisition_error(exc)
                logger.error(
                    event="sop_request_variable_tool_failed",
                    variable_name=variable_name,
                    acquisition_tool=acquisition_tool,
                    conversation_id=conversation_id,
                    error_type=error_type,
                    error=str(exc),
                )
        if acquired_value is not None:
            logger.info(
                event="sop_request_variable_tool_acquired",
                variable_name=variable_name,
                acquisition_tool=acquisition_tool,
            )
            # 写回变量池，后续依赖变量可直接从池中读取
            await _persist_variable(conversation_sop_client, conversation_id, variable_name, acquired_value)
            return {"ok": True, "value": acquired_value, "source": "tool_call"}
        # 工具执行成功但未取到值：按返回值归类
        if not failure_reason:
            failure_reason = f"工具 {acquisition_tool} 已执行但取值为空"
            error_type = _classify_tool_result_error(locals().get("tool_result"))
        logger.info(
            event="sop_request_variable_escalate_to_human",
            conversation_id=conversation_id,
            variable_name=variable_name,
            acquisition_tool=acquisition_tool,
            error_type=error_type,
        )
        return await _request_user_input(
            var_schema=var_def,
            kind="variable_input",
            msg=(
                f"变量 {variable_name} 无法自动获取（{error_type}）：{failure_reason}。"
                "请人工提供该变量值或确认后继续。"
            ),
        )

    if strategy == STRATEGY_SKILL_CALL and acquisition_tool:
        # skill_call 策略（含 skill:xxx 冒号简写）：执行数据库动态 Skill 计算变量值
        # 失败即接管：以下任一采集失败均一律转人工弹框，绝不将泛化 error 交回模型触发手工兜底。
        async def _escalate_skill(msg: str, error_type: str) -> VariableRequestResult:
            logger.info(
                event="sop_request_variable_escalate_to_human",
                conversation_id=conversation_id,
                variable_name=variable_name,
                acquisition_skill=acquisition_tool,
                error_type=error_type,
            )
            return await _request_user_input(
                var_schema=var_def,
                kind="variable_input",
                msg=msg,
            )

        if skill_runner is None:
            logger.error(
                event="sop_request_variable_skill_runner_missing",
                variable_name=variable_name,
                acquisition_skill=acquisition_tool,
                conversation_id=conversation_id,
                error_type="executor_unavailable",
            )
            return await _escalate_skill(
                f"变量 {variable_name} 声明由 Skill {acquisition_tool} 获取，但运行时未注入 DynamicSkillRunner，无法自动取值。请人工提供该变量值或确认后继续。",
                "executor_unavailable",
            )
        try:
            skill_context = _unwrap_context_variables(context_variables)
            skill_result = await skill_runner.execute(
                acquisition_tool,
                skill_context,
                variable_name=variable_name,
                output_path=output_path,
                reason=reason,
                conversation_id=conversation_id,
            )
            acquired_value = _extract_value(skill_result, variable_name, "value")
            if acquired_value is not None:
                logger.info(
                    event="sop_request_variable_skill_executed",
                    variable_name=variable_name,
                    acquisition_skill=acquisition_tool,
                )
                await _persist_variable(conversation_sop_client, conversation_id, variable_name, acquired_value)
                return {
                    "ok": True,
                    "value": acquired_value,
                    "source": "skill_call",
                    "skill_name": skill_result.get("skill_name")
                    if isinstance(skill_result, dict)
                    else acquisition_tool,
                }
            # Skill 执行成功但未取到值 → 失败即接管转人工
            return await _escalate_skill(
                f"变量 {variable_name} 由 Skill {acquisition_tool} 执行成功但未产出取值，无法自动获取。请人工提供该变量值或确认后继续。",
                "empty_value",
            )
        except SkillNotFoundError as exc:
            # Agent/技能域：技能确实不存在或未启用
            logger.error(
                event="sop_request_variable_skill_not_found",
                variable_name=variable_name,
                acquisition_skill=acquisition_tool,
                error_type="tool_capability_missing",
                error=str(exc),
            )
            return await _escalate_skill(
                f"变量 {variable_name} 自动获取失败（tool_capability_missing：{exc.user_message}）。请人工提供该变量值或确认后继续。",
                "tool_capability_missing",
            )
        except SkillLLMError as exc:
            # LLM/提供商域：与技能配置无关，明确告知后端问题
            logger.error(
                event="sop_request_variable_skill_llm_failed",
                variable_name=variable_name,
                acquisition_skill=acquisition_tool,
                llm_code=exc.llm_code,
                error_type="tool_error",
                error=str(exc),
            )
            return await _escalate_skill(
                f"变量 {variable_name} 自动获取失败（skill_llm_failed：{exc.user_message}）。请人工提供该变量值或确认后继续。",
                "tool_error",
            )
        except SkillOutputUnavailableError as exc:
            # Agent/技能域：技能执行成功但未产出所请求的变量（指令 / output_path 配置问题）
            logger.error(
                event="sop_request_variable_skill_output_unavailable",
                variable_name=variable_name,
                acquisition_skill=acquisition_tool,
                error_type="empty_value",
                error=str(exc),
            )
            return await _escalate_skill(
                f"变量 {variable_name} 自动获取失败（skill_output_unavailable：{exc.user_message}）。请人工提供该变量值或确认后继续。",
                "empty_value",
            )
        except SkillError as exc:
            # Agent/技能域兜底（工具依赖缺失、AI 客户端缺失等）
            logger.error(
                event="sop_request_variable_skill_failed",
                variable_name=variable_name,
                acquisition_skill=acquisition_tool,
                error_type=_classify_acquisition_error(exc),
                error=str(exc),
            )
            return await _escalate_skill(
                f"变量 {variable_name} 自动获取失败（skill_failed：{exc.user_message}）。请人工提供该变量值或确认后继续。",
                _classify_acquisition_error(exc),
            )

    if strategy == STRATEGY_JSON_EXTRACT:
        import json

        # 失败即接管：json_extract 任何采集/配置失败均转人工弹框，不交回模型；
        # 保留“前置依赖未就绪”的引导返回（该引导指向 sop_request_variable 受控工具，非手工命令）。
        async def _escalate_json(msg: str, error_type: str) -> VariableRequestResult:
            logger.info(
                event="sop_request_variable_escalate_to_human",
                conversation_id=conversation_id,
                variable_name=variable_name,
                error_type=error_type,
            )
            return await _request_user_input(
                var_schema=var_def,
                kind="variable_input",
                msg=(f"变量 {variable_name} 无法自动提取（{error_type}：{msg}）。请人工提供该变量值或确认后继续。"),
            )

        try:
            from jsonpath_ng.ext import parse as jsonpath_parse
        except ImportError:
            return await _escalate_json(
                "json_extract 依赖 jsonpath-ng 未安装", "tool_capability_missing"
            )

        # 1. 检验 depends_on 前置依赖
        if not depends_on:
            return await _escalate_json(
                f"变量 {variable_name} 的策略为 json_extract，但未声明 depends_on 父变量", "contract_error"
            )

        dependency_name = depends_on[0]
        dependency_payload = context_variables.get(dependency_name)
        if not dependency_payload:
            # 流程引导（非失败）：告知先调用 sop_request_variable 采集依赖，仍属受控链路，保留返回
            return {
                "error": "sop_variable_dependency_missing",
                "message": f"变量 {variable_name} 的 json_extract 前置依赖 {dependency_name} 尚未就绪，请先调用 sop_request_variable(variable_name='{dependency_name}')",
                "variable_name": variable_name,
                "missing_dependencies": [dependency_name],
            }

        # 2. 提取 exec_id，尝试从 Redis 取完整原始数据
        exec_id_for_cache = dependency_payload.get("exec_id") if isinstance(dependency_payload, dict) else None

        raw_data_str: str | None = None

        if exec_id_for_cache:
            try:
                redis_client = getattr(tool_executor, "_redis", None)
                if redis_client:
                    cache_key = f"cmd_cache:{exec_id_for_cache}"
                    cached_bytes = await redis_client.client.get(cache_key)
                    if cached_bytes:
                        raw_data_str = cached_bytes.decode("utf-8") if isinstance(cached_bytes, bytes) else cached_bytes
                        logger.info(
                            event="json_extract_cache_hit",
                            variable_name=variable_name,
                            exec_id=exec_id_for_cache,
                            raw_size=len(raw_data_str),
                        )
            except Exception as redis_err:
                logger.warning(
                    event="json_extract_redis_failed",
                    variable_name=variable_name,
                    error=str(redis_err),
                )

        # 3. 缓存穿透兜底：Redis 无数据时退化使用已截断的 value
        if not raw_data_str:
            raw_data_str = (
                dependency_payload.get("value") if isinstance(dependency_payload, dict) else str(dependency_payload)
            )
            logger.warning(
                event="json_extract_cache_miss_fallback",
                variable_name=variable_name,
                note="Redis 缓存已失效，使用已截断数据降级提取，结果可能不完整",
            )

        if not raw_data_str:
            return await _escalate_json(f"前置依赖 {dependency_name} 的数据内容为空", "empty_value")

        # 4. 解析 JSON
        try:
            json_data = json.loads(raw_data_str)
        except json.JSONDecodeError as je:
            return await _escalate_json(f"依赖变量 {dependency_name} 的输出非合法 JSON: {str(je)}", "tool_error")

        # 5. 渲染 expression 中的变量占位符
        expression_str = var_def.get("expression", "")
        if not expression_str:
            return await _escalate_json("json_extract 策略未指定 expression（JSONPath）", "contract_error")

        unwrapped_ctx = _unwrap_context_variables(context_variables)
        try:
            expression_str = expression_str.format(**unwrapped_ctx)
        except KeyError as ke:
            return await _escalate_json(f"expression 占位符 {ke} 对应的上下文变量未就绪", "contract_error")

        # 6. JSONPath 匹配
        try:
            jsonpath_expr = jsonpath_parse(expression_str)
        except Exception as parse_err:
            return await _escalate_json(f"JSONPath 表达式语法错误: {expression_str} — {str(parse_err)}", "contract_error")

        matches = [m.value for m in jsonpath_expr.find(json_data)]

        if not matches:
            return await _escalate_json(
                (
                    f"在 {dependency_name} 的{'完整' if exec_id_for_cache else '截断'}数据中，"
                    f"使用 JSONPath `{expression_str}` 未匹配到任何结果"
                ),
                "empty_value",
            )

        extracted_value = matches[0]
        logger.info(
            event="json_extract_success",
            variable_name=variable_name,
            expression=expression_str,
            extracted_value=str(extracted_value)[:100],
        )
        return {"ok": True, "value": extracted_value, "source": "json_extract"}

    if strategy == STRATEGY_USER_CONFIRM:
        # user_confirm 策略：先调用工具获取候选值，再展示给用户确认
        options: list[dict] = []
        if acquisition_tool and tool_executor is not None:
            try:
                candidates_result = await tool_executor.execute(acquisition_tool, {}, conversation_id=conversation_id)
                if isinstance(candidates_result, list):
                    options = [{"optionId": str(item), "name": str(item)} for item in candidates_result]
                elif isinstance(candidates_result, dict) and "items" in candidates_result:
                    options = [{"optionId": str(item), "name": str(item)} for item in candidates_result["items"]]
                logger.info(
                    event="sop_request_variable_confirm_candidates",
                    variable_name=variable_name,
                    acquisition_tool=acquisition_tool,
                    candidate_count=len(options),
                )
            except Exception as exc:
                logger.warning(
                    event="sop_request_variable_confirm_fetch_failed",
                    variable_name=variable_name,
                    acquisition_tool=acquisition_tool,
                    error=str(exc),
                )
        # 展示候选值（可能为空）让用户确认
        return await _request_user_input(
            var_schema=var_def,
            kind="variable_confirm",
            options=options,
            msg=f"变量 {variable_name} 需要用户确认",
        )

    # 注意：旧策略别名（"tool"、"env_context" 等）已在 parse_strategy() 中统一归一，
    # 不再需要此处的单独兼容分支。

    # 默认：user_input 策略
    return await _request_user_input(
        var_schema=var_def,
        kind="variable_input",
        msg=f"变量 {variable_name} 需要用户提供值",
    )
