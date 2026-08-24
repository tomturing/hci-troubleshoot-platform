"""Agent 可执行 Capability 运行时发现。

该端点只报告当前进程内真实导入的 Validator/Handler 和执行桥状态，不读取 Admin 可
编辑配置，也不把“Schema 已声明”冒充为“现场可执行”。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from shared.schemas.acquirer_args import (
    CONDITIONAL_PRODUCERS,
    DEFAULT_SIGNAL_TIMEOUT_SECONDS,
    FRONTEND_TOOLS,
)

from app.config import settings
from app.tools.qfk.handlers import HandlerRegistry, build_acli_command
from app.tools.qfk.signal import BackendSignal
from app.tools.qkv.signal import FrontendSignal

router = APIRouter(prefix="/internal", tags=["capabilities"])


class QfkCommandPreviewRequest(BaseModel):
    """只读的 QFK 命令编译请求；请求不会进入执行器。"""

    signal: dict[str, Any]


def _check_internal_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header.split(" ", 1)[1] != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="内部 Token 无效")


def runtime_capability_document() -> dict:
    """从当前 Agent 进程构造确定性的 Handler/Validator 部署快照。"""

    from app.tools.acli import executor as executor_module
    from app.tools.effect.adapter import effect_enabled
    from app.tools.vm_console.adapter import capture_enabled

    bridge_ready = executor_module._executor is not None
    # QKV 工具清单从共享契约集合派生（防硬编码漏改）：直接生产者 + 条件型生产者。
    qkv_tools = tuple(sorted(FRONTEND_TOOLS)) + tuple(sorted(CONDITIONAL_PRODUCERS))
    qfk_tools = (*tuple(f"qfk_{namespace}" for namespace in HandlerRegistry.supported_namespaces()), "qfk_var")
    capabilities = []
    for capability_id in (*qkv_tools, *qfk_tools):
        producer = capability_id.startswith("qkv_")
        conditional = capability_id in CONDITIONAL_PRODUCERS
        is_effect = capability_id == "qkv_effect"
        is_variable_processor = capability_id == "qfk_var"
        handler_ready = (
            producer
            or is_variable_processor
            or capability_id.removeprefix("qfk_") in HandlerRegistry.supported_namespaces()
        )
        validator_ready = FrontendSignal is not None if producer else BackendSignal is not None
        executor_ready = True if is_variable_processor else bridge_ready
        usable = handler_ready and validator_ready and executor_ready
        reason = None if usable else "Terminal Bridge Executor 尚未注入，参数可校验但暂不可执行"
        if conditional and is_effect:
            # 条件型效果验证生产者：执行层受 EFFECT_VERIFICATION_ENABLED 策略门禁控制。
            enabled = effect_enabled()
            usable = usable and enabled
            if not enabled:
                reason = "条件型效果验证生产者：执行层未启用（EFFECT_VERIFICATION_ENABLED=false），契约与发布不受影响"
        elif conditional:
            # 条件型实时视觉生产者：执行层受 VM_CONSOLE_CAPTURE_ENABLED 策略门禁控制。
            enabled = capture_enabled()
            usable = usable and enabled
            if not enabled:
                reason = "条件型视觉生产者：执行层未启用（VM_CONSOLE_CAPTURE_ENABLED=false），契约与发布不受影响"
        capabilities.append(
            {
                "capability_id": capability_id,
                "implemented": handler_ready,
                "deployed": True,
                "validator_ready": validator_ready,
                "executor_ready": executor_ready,
                "usable": usable,
                "runtime_status": "available" if usable else "degraded",
                "reason": reason,
                # 条件生产者标记：vm_console 需可信 HOST/VM_ID 且唤醒为受控交互（非只读）；
                # effect 的先决变量随期望锚点动态声明，且严格只读、无受控交互。
                "conditional_producer": conditional,
                "controlled_interaction": conditional and not is_effect,
            }
        )
    return {
        "schema_version": 1,
        "service": settings.SERVICE_NAME,
        "capabilities": capabilities,
        "count": len(capabilities),
    }


def compile_qfk_command_preview(raw_signal: dict[str, Any]) -> dict[str, Any]:
    """用运行中 Agent 的 Handler 编译一条 QFK 信号，供专家审核命令模板。

    这里必须复用 ``BackendSignal`` 和 ``build_acli_command``，不能由 Admin UI
    自行拼接 aCLI。这样 ``--container``、``--cluster``、日志 Catalog 和 shell
    参数转义与真正的运行时路径保持同源。变量占位符（如 ``{{PID}}``）刻意保留，
    它们会在实际执行前由变量池替换；host 则是 SSH 路由字段，不属于 aCLI 命令。
    """

    acquire = raw_signal.get("acquire") if isinstance(raw_signal, dict) else None
    if not isinstance(acquire, dict):
        raise ValueError("signal.acquire 必须是对象")
    tool = str(acquire.get("tool") or "").strip()
    if not tool.startswith("qfk_") or tool == "qfk_":
        raise ValueError("仅支持预览 QFK 消费者信号")
    namespace = tool.removeprefix("qfk_")
    if tool == "qfk_var":
        return {
            "tool": tool,
            "commands": [],
            "execution": "variable_pool",
            "message": "qfk_var 是纯变量处理器，不生成或执行 aCLI 命令",
        }
    if namespace not in HandlerRegistry.supported_namespaces():
        raise ValueError(f"未声明的 QFK 采集类型: {tool}")

    args = acquire.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("signal.acquire.args 必须是对象")
    matcher = raw_signal.get("match") or {}
    if not isinstance(matcher, dict):
        raise ValueError("signal.match 必须是对象或 null")

    pattern = matcher.get("pattern") if matcher.get("type") == "keyword" else None
    keywords = (
        [pattern] if isinstance(pattern, str) and pattern else list(pattern or []) if isinstance(pattern, list) else []
    )
    filter_keywords = _extract_filter_keywords(raw_signal)
    data: dict[str, Any] = {
        "namespace": namespace,
        "keyword": keywords,
        "match_mode": {"any": "or", "all": "and"}.get(
            str(matcher.get("mode") or "or").lower(),
            str(matcher.get("mode") or "or").lower(),
        ),
        "expected": bool(matcher.get("expected", True)),
        "instruction": args.get("instruction"),
        "host": args.get("host"),
        "timeout": args.get("timeout", DEFAULT_SIGNAL_TIMEOUT_SECONDS),
        "command": args.get("command"),
        "command_args": args.get("command_args") or [],
        "container": args.get("container", "asv" if namespace == "service" else None),
        "cluster": bool(args.get("cluster", False)),
        "formatter": args.get("formatter"),
        "resource_keyword": args.get("resource_keyword"),
        "file": args.get("file"),
        "path": args.get("path"),
        "time_window": args.get("time_window"),
        "source_family": args.get("source_family", "auto"),
        "parser": args.get("parser"),
        "request_id": args.get("request_id"),
        "context_lines": args.get("context_lines", 0),
        "include_archives": args.get("include_archives", False),
        "archive_precheck": args.get("archive_precheck"),
        "matcher": matcher or None,
        "filter_keywords": filter_keywords,
    }
    if namespace == "service":
        data["service"] = args.get("service") or args.get("resource_keyword")
        data["action"] = args.get("action") or args.get("command") or "status"

    signal = BackendSignal.from_dict(data)
    return {
        "tool": tool,
        "command": build_acli_command(signal),
        "host": signal.host,
        "variables": sorted({item for value in (args, matcher) for item in _find_placeholders(value)}),
        "notice": "这是由当前 Agent Handler 编译的只读命令模板；变量会在执行前替换，host 仅用于选择 SSH 目标主机。",
    }


def _find_placeholders(value: Any) -> set[str]:
    """递归找出命令模板中的变量，避免审核者误以为占位符会原样执行。"""

    import re

    if isinstance(value, str):
        return set(re.findall(r"\{\{([A-Z][A-Z0-9_]*)", value))
    if isinstance(value, dict):
        return set().union(*(_find_placeholders(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_find_placeholders(item) for item in value)) if value else set()
    return set()


def _extract_filter_keywords(signal: dict[str, Any]) -> list[str]:
    """从 Matcher/Produce 的唯一 Extract 事实派生日志命令粗筛，不新增持久化 keyword。"""

    specs: list[Any] = []
    matcher = signal.get("match")
    if isinstance(matcher, dict):
        specs.append(matcher.get("extract"))
    orchestrate = signal.get("orchestrate")
    if isinstance(orchestrate, dict):
        specs.extend(item.get("extract") for item in orchestrate.get("produces") or [] if isinstance(item, dict))
    result: list[str] = []
    for spec in specs:
        rows = spec.get("rows") if isinstance(spec, dict) else None
        if not isinstance(rows, dict) or rows.get("mode") != "keywords":
            continue
        for value in rows.get("include") or []:
            literal = str(value).strip()
            if literal and literal not in result:
                result.append(literal)
    return result


@router.get("/capabilities")
async def get_runtime_capabilities(request: Request) -> dict:
    """返回当前 Agent Pod 的真实 Capability 部署状态。"""

    _check_internal_auth(request)
    return runtime_capability_document()


@router.post("/qfk-command-preview")
async def get_qfk_command_preview(request: Request, body: QfkCommandPreviewRequest) -> dict[str, Any]:
    """返回当前 QFK 信号的完整 aCLI 命令模板，不触发任何命令执行。"""

    _check_internal_auth(request)
    try:
        return compile_qfk_command_preview(body.signal)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
