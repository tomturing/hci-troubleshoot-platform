"""qkv_vm_console 在线专用适配器（设计文档 §5.1/§5.4/§8）。

职责：Inventory 校验 → 创建截图会话 → 通过受控 Bridge 通道执行固定截图操作 →
确定性近黑检测 → 受控唤醒确认（每运行最多一次）→ 视觉提取 → 变量回写。

⚠️ 安全边界：
- 只消费 VmConsoleResolver 编译出的不可变 Capture Intent，绝不拼接命令字符串；
- 截图与唤醒是两个独立 operation，唤醒必须已记录用户确认才可调度；
- 所有失败默认拒绝（fail-closed），错误码使用 §8 全集，不压缩为"采集失败"。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from shared.observability.logger import get_logger
from shared.observability.metrics import (
    VM_CONSOLE_CAPTURE_DURATION_SECONDS,
    VM_CONSOLE_CAPTURE_TOTAL,
    VM_CONSOLE_NEAR_BLACK_TOTAL,
    VM_CONSOLE_SECURITY_REJECTION_TOTAL,
    VM_CONSOLE_VISION_TOTAL,
    VM_CONSOLE_WAKE_TOTAL,
    vm_console_confidence_band,
)
from shared.observability.otel import get_current_trace_id
from shared.resolution.models import ResolutionStatus, SignalIntent
from shared.resolution.runtime import get_resolution_runtime
from shared.resolution.vm_console import (
    VM_CONSOLE_RESOLVER_ID,
    VM_CONSOLE_TOOL,
    VmConsoleCaptureIntent,
    capture_intent_from,
)
from shared.utils.internal_http import InternalHTTPClient

from app.tools.vm_console import store
from app.tools.vm_console.inventory import verify_vm_target

logger = get_logger("vm-console-adapter")

ADAPTER_VERSION = "vm-console-adapter-v1"
# §10 fail-closed 开关：§12 平台确认项闭环前默认关闭执行层（契约层不受影响）。
CAPTURE_ENABLED_ENV = "VM_CONSOLE_CAPTURE_ENABLED"
# 唤醒确认等待窗口（秒）：超时按拒绝处理并继续基线识图。
WAKE_CONFIRM_TIMEOUT_SECONDS = int(os.getenv("VM_CONSOLE_WAKE_CONFIRM_TIMEOUT_SECONDS", "300"))
# 唤醒后等待画面稳定的固定窗口（秒），随后重截图；不得循环重试。
WAKE_SETTLE_SECONDS = int(os.getenv("VM_CONSOLE_WAKE_SETTLE_SECONDS", "5"))


def capture_enabled() -> bool:
    return os.environ.get(CAPTURE_ENABLED_ENV, "false").lower() in ("1", "true", "yes", "on")


def _redis_client():
    """复用 Bridge Relay 执行器已注入的 RedisManager（lifespan 期注入）。"""

    from app.tools.acli import executor as executor_module

    bridge_executor = executor_module._executor
    if bridge_executor is None or getattr(bridge_executor, "_redis", None) is None:
        raise RuntimeError("Terminal Bridge Executor 尚未注入，无法等待截图结果")
    return bridge_executor._redis.client


@dataclass
class VmConsoleCaptureResult:
    """适配器输出（字段与 QKVResult 对齐，供 _fill_pool_from_qkv 消费）。"""

    success: bool
    query: str = "vm_console"
    keyword: str = ""
    command: str = "vm_console_capture://fixed-operation"  # 固定意图标识，非可执行命令
    values: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    exec_id: str | None = None
    capture_id: str | None = None
    resolution: dict[str, Any] = field(default_factory=dict)

    def to_observation(self) -> str:
        if not self.success:
            return f"虚拟机控制台截图失败（{self.error_code or 'UNKNOWN'}）: {self.error or '未知错误'}"
        if not self.values:
            return "虚拟机控制台截图完成，但未获得有效视觉观察"
        first = self.values[0]
        state = first.get("vm_console_state", "unknown")
        summary = first.get("vm_console_summary", "")
        confidence = first.get("vm_console_confidence", 0.0)
        return (
            f"虚拟机控制台截图完成：画面状态={state}，置信度={confidence}。"
            f"可见现象：{summary or '无补充描述'}。"
            "注意：视觉观察仅为证据之一，不能单独证明根因。"
        )


async def _push_bridge_op(
    http_client: InternalHTTPClient,
    *,
    conversation_id: str,
    capture_id: str,
    exec_id: str,
    operation: str,
    host_node_id: str,
    vm_id: str,
    node_ip: str | None,
    case_id: str,
    timeout_seconds: int,
    intent: VmConsoleCaptureIntent,
    role: str,
    trace_id: str,
) -> dict[str, Any]:
    """把固定操作经 conversation-service 中继到 terminal_bridge，并等待元数据结果。

    二进制 PPM 由 Bridge 直传 conversation-service 制品端点（不经 WS/Redis）；
    这里只传递不可变 Capture Intent 的受限字段。
    """

    resp = await http_client.post(
        f"/internal/conversations/{conversation_id}/vm-console-op",
        json={
            "capture_id": capture_id,
            "exec_id": exec_id,
            "operation": operation,
            "host_node_id": host_node_id,
            "vm_id": vm_id,
            "node_ip": node_ip,
            "case_id": case_id,
            "timeout_seconds": timeout_seconds,
            "role": role,  # baseline | recapture
            "artifact_policy": intent.artifact_policy,
            "catalog_revision": intent.catalog_revision,
            "trace_id": trace_id,
        },
    )
    resp.raise_for_status()
    push_result = resp.json()
    if not push_result.get("ok"):
        return {"failed": True, "error": push_result.get("message", "推送截图操作失败"), "error_type": "push_failed"}

    redis = _redis_client()
    result_key = f"vm_console_result:{exec_id}"
    raw = await redis.blpop(result_key, timeout=timeout_seconds + 30)
    if raw is None:
        return {"failed": True, "error": f"等待截图结果超时（{timeout_seconds + 30}s）", "error_type": "timeout"}
    try:
        payload = json.loads(raw[1])
    except (json.JSONDecodeError, IndexError):
        return {"failed": True, "error": "截图结果不可解析", "error_type": "invalid_result"}
    return payload


async def run_vm_console_signal(
    signal: dict[str, Any],
    env_context: dict[str, str],
    *,
    conversation_id: str,
    case_id: str,
    session_id: str = "",
    exec_id: str | None = None,
    db_session_factory: Any,
    scp_client: Any | None = None,
    user_id: str | None = None,
) -> VmConsoleCaptureResult:
    """执行 qkv_vm_console 信号的在线端到端编排（基线截图 + 可选唤醒重截 + 视觉提取）。"""

    trace_id = get_current_trace_id() or ""
    args = ((signal.get("acquire") or {}).get("args") or {})
    signal_id = str(signal.get("id") or "")
    host = str(args.get("host") or "").strip()
    vm_id = str(args.get("vm_id") or "").strip()
    timeout_seconds = int(args.get("timeout") or 60)
    timeout_seconds = max(1, min(timeout_seconds, 60))
    produces = ((signal.get("orchestrate") or {}).get("produces") or [])

    # 0. 策略开关：§12 平台确认项闭环前默认关闭执行层。
    if not capture_enabled():
        return VmConsoleCaptureResult(
            success=False,
            error="虚拟机控制台截图能力未启用（VM_CONSOLE_CAPTURE_ENABLED=false）",
            error_code="VM_CONSOLE_DISABLED_BY_POLICY",
        )

    # 1. Shared Resolution Runtime 编译不可变 Capture Intent（fail-closed）。
    runtime = get_resolution_runtime()
    plan, acquisition = runtime.compile_and_resolve(
        SignalIntent(
            resolver_id=VM_CONSOLE_RESOLVER_ID,
            tool=VM_CONSOLE_TOOL,
            args={"host": host, "vm_id": vm_id, "capture_mode": args.get("capture_mode"), "timeout": timeout_seconds},
        ),
        {"variables": {k: v for k, v in env_context.items()}},
    )
    if plan.status is ResolutionStatus.BLOCKED or acquisition.status is ResolutionStatus.BLOCKED:
        issues = "; ".join(issue.message for issue in (plan.issues + acquisition.issues))
        return VmConsoleCaptureResult(
            success=False,
            error=f"Capture Intent 编译被拒绝: {issues}",
            error_code="TARGET_CONTEXT_MISSING",
            resolution={"issues": issues},
        )
    intent = capture_intent_from(acquisition)
    if intent is None:
        return VmConsoleCaptureResult(
            success=False, error="Capture Intent 不可还原", error_code="TARGET_CONTEXT_MISSING"
        )

    # 2. Inventory 归属校验（任一环节不可验证即拒绝执行）。
    verification = await verify_vm_target(intent.host_ref, intent.vm_ref, scp_client=scp_client)
    if not verification.verified:
        code = (
            "TARGET_OWNERSHIP_MISMATCH"
            if "MISMATCH" in verification.reason or "归属" in verification.reason
            else "TARGET_CONTEXT_MISSING"
        )
        VM_CONSOLE_SECURITY_REJECTION_TOTAL.labels(reason=code.lower(), mode="online").inc()
        VM_CONSOLE_CAPTURE_TOTAL.labels(stage="inventory", status="failed", mode="online").inc()
        try:
            async with db_session_factory() as audit_session:
                await store.insert_audit_event(
                    audit_session, capture_id=None, event_type="target_rejected", case_id=case_id,
                    conversation_id=conversation_id, actor="system:inventory", trace_id=trace_id,
                    detail={"reason": code, "host": verification.host_node_id, "vm_id": verification.vm_id},
                )
        except Exception as exc:
            logger.warning("vm_console_audit_write_failed", event="target_rejected", error=str(exc))
        return VmConsoleCaptureResult(
            success=False,
            error=verification.reason,
            error_code=code,
            resolution={"target_verification": verification.__dict__},
        )

    node_ip = str(env_context.get("node_ip") or "").strip() or None
    capture_id = str(uuid.uuid4())
    http_client = InternalHTTPClient(service_name="conversation-service")
    run_exec_id = exec_id or f"vmc-{uuid.uuid4().hex[:16]}"

    # 3. 创建不可变截图会话记录。
    async with db_session_factory() as session:
        await store.create_capture_record(
            session,
            capture_id=capture_id,
            tenant_id=None,
            case_id=case_id,
            diagnosis_run_id=session_id or None,
            conversation_id=conversation_id,
            signal_id=signal_id or None,
            host_node_id=verification.host_node_id,
            vm_id=verification.vm_id,
            target_verification={
                "source": verification.source,
                "detail": verification.detail,
                "adapter_version": ADAPTER_VERSION,
            },
            source_kbd_id=None,
            source_kbd_revision=None,
            tool_catalog_revision=plan.catalog_version,
            adapter_version=ADAPTER_VERSION,
            trace_id=trace_id,
            exec_id=run_exec_id,
        )
        await store.update_capture_status(session, capture_id, "inventory_verified")
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="requested", case_id=case_id,
            conversation_id=conversation_id, actor="system:adapter", trace_id=trace_id,
            detail={"signal_id": signal_id, "host_node_id": verification.host_node_id, "vm_id": verification.vm_id},
        )
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="target_verified", case_id=case_id,
            conversation_id=conversation_id, actor="system:inventory", trace_id=trace_id,
            detail={"source": verification.source},
        )

    # 4. 基线截图（固定 operation；PPM 由 Bridge 直传制品端点）。
    async with db_session_factory() as session:
        await store.update_capture_status(session, capture_id, "baseline_capturing")
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="baseline_capturing", case_id=case_id,
            conversation_id=conversation_id, actor="system:bridge", trace_id=trace_id,
        )
    baseline_start = time.monotonic()
    baseline = await _push_bridge_op(
        http_client,
        conversation_id=conversation_id,
        capture_id=capture_id,
        exec_id=run_exec_id,
        operation="capture_baseline",
        host_node_id=verification.host_node_id,
        vm_id=verification.vm_id,
        node_ip=node_ip,
        case_id=case_id,
        timeout_seconds=timeout_seconds,
        intent=intent,
        role="baseline",
        trace_id=trace_id,
    )
    VM_CONSOLE_CAPTURE_DURATION_SECONDS.labels(stage="baseline", mode="online").observe(
        time.monotonic() - baseline_start
    )
    if baseline.get("failed") or baseline.get("error_type"):
        error_type = str(baseline.get("error_type") or "")
        code = (
            "BASELINE_CAPTURE_FAILED"
            if error_type in {"target_invalid", "operation_invalid", "exec_failed", "timeout", "push_failed"}
            else "ARTIFACT_UPLOAD_FAILED"
            if "upload" in error_type or error_type == "artifact_upload_disabled"
            else "MONITOR_UNAVAILABLE"
        )
        async with db_session_factory() as session:
            await store.update_capture_status(
                session, capture_id, "failed", error_code=code, error_summary=str(baseline.get("error") or error_type)
            )
        VM_CONSOLE_CAPTURE_TOTAL.labels(stage="baseline", status="failed", mode="online").inc()
        async with db_session_factory() as session:
            await store.insert_audit_event(
                session, capture_id=capture_id, event_type="failed", case_id=case_id,
                conversation_id=conversation_id, actor="system:bridge", trace_id=trace_id,
                detail={"stage": "baseline", "error_code": code},
            )
        # 失败也推送结果卡事件：客户端展示可行动原因，不泄露内部 URI/令牌细节。
        try:
            await http_client.post(
                f"/internal/conversations/{conversation_id}/vm-console-observation",
                json={
                    "capture_id": capture_id,
                    "observation_status": "unavailable",
                    "display_state": "unknown",
                    "summary": f"控制台截图未完成（{code}），可检查 Bridge 连接与目标 VM 状态后重试",
                    "confidence": 0.0,
                    "error_code": code,
                },
            )
        except Exception as exc:
            logger.warning("vm_console_failure_push_failed", capture_id=capture_id, error=str(exc))
        return VmConsoleCaptureResult(
            success=False, error=str(baseline.get("error") or "基线截图失败"), error_code=code,
            capture_id=capture_id, exec_id=run_exec_id,
        )

    near_black = bool(baseline.get("near_black"))
    quality = baseline.get("quality") or {}
    VM_CONSOLE_CAPTURE_TOTAL.labels(stage="baseline", status="ok", mode="online").inc()
    VM_CONSOLE_NEAR_BLACK_TOTAL.labels(near_black=str(near_black).lower(), mode="online").inc()
    async with db_session_factory() as session:
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="quality_checked", case_id=case_id,
            conversation_id=conversation_id, actor="system:quality", trace_id=trace_id,
            detail={
                "near_black": near_black,
                "artifact_sha256": str(baseline.get("sha256") or ""),
                "algorithm_revision": (quality.get("metrics") or {}).get("algorithm_revision"),
            },
        )
    artifact_id = str(baseline.get("artifact_id") or "")
    async with db_session_factory() as session:
        await store.update_capture_status(
            session,
            capture_id,
            "quality_checked",
            quality_metrics=quality,
            baseline_artifact_id=artifact_id or None,
            effective_artifact_id=artifact_id or None,
        )

    # 5. 近黑 → 申请唤醒确认（每运行最多一次；拒绝/超时继续基线识图）。
    wake_executed = False
    if near_black:
        wake_granted = await _request_wake_confirmation(
            http_client,
            db_session_factory=db_session_factory,
            capture_id=capture_id,
            case_id=case_id,
            conversation_id=conversation_id,
            host_node_id=verification.host_node_id,
            vm_id=verification.vm_id,
            user_id=user_id,
        )
        if wake_granted:
            recapture = await run_wake_and_recapture(
                http_client,
                db_session_factory=db_session_factory,
                capture_id=capture_id,
                conversation_id=conversation_id,
                case_id=case_id,
                host_node_id=verification.host_node_id,
                vm_id=verification.vm_id,
                node_ip=node_ip,
                timeout_seconds=timeout_seconds,
                intent=intent,
                trace_id=trace_id,
            )
            wake_executed = recapture.get("success", False)
            VM_CONSOLE_WAKE_TOTAL.labels(
                decision="confirmed", result="success" if wake_executed else "failed", mode="online"
            ).inc()
            if wake_executed and recapture.get("artifact_id"):
                artifact_id = str(recapture["artifact_id"])

    if not near_black:
        VM_CONSOLE_WAKE_TOTAL.labels(decision="not_needed", result="not_attempted", mode="online").inc()

    # 6. 视觉提取（策略门禁在 extractor 内；失败降级 unavailable 而非报错）。
    from app.tools.vm_console.vision_extractor import extract_observation

    ppm_bytes = await _fetch_artifact_bytes(http_client, artifact_id)
    if ppm_bytes is None:
        async with db_session_factory() as session:
            await store.update_capture_status(
                session, capture_id, "failed",
                error_code="ARTIFACT_UPLOAD_FAILED", error_summary="制品读取失败，无法执行视觉提取",
            )
        return VmConsoleCaptureResult(
            success=False, error="截图制品不可读取", error_code="ARTIFACT_UPLOAD_FAILED",
            capture_id=capture_id, exec_id=run_exec_id,
        )

    async with db_session_factory() as session:
        await store.update_capture_status(session, capture_id, "vision_analyzing")
    vision_start = time.monotonic()
    observation = await extract_observation(ppm_bytes, artifact_id=artifact_id, trace_id=trace_id)
    observation_payload = observation.model_dump(mode="json")
    VM_CONSOLE_CAPTURE_DURATION_SECONDS.labels(stage="vision", mode="online").observe(
        time.monotonic() - vision_start
    )
    VM_CONSOLE_VISION_TOTAL.labels(
        state=observation.display_state,
        confidence_band=vm_console_confidence_band(observation.confidence),
        mode="online",
    ).inc()
    VM_CONSOLE_CAPTURE_TOTAL.labels(stage="vision", status="ok", mode="online").inc()
    async with db_session_factory() as session:
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="vision_completed", case_id=case_id,
            conversation_id=conversation_id, actor="system:vision", trace_id=trace_id,
            detail={
                "display_state": observation.display_state,
                "confidence": observation.confidence,
                "model_revision": observation.model_revision,
                "artifact_id": observation.artifact_id,
            },
        )

    # 推送完成态观察事件（客户端结果卡数据源）；失败不影响变量回写。
    try:
        await http_client.post(
            f"/internal/conversations/{conversation_id}/vm-console-observation",
            json={
                "capture_id": capture_id,
                "observation_status": observation.observation_status,
                "display_state": observation.display_state,
                "summary": observation.summary,
                "confidence": observation.confidence,
                "needs_human_review": observation.needs_human_review,
                "artifact_id": observation.artifact_id,
                "wake_executed": wake_executed,
            },
        )
    except Exception as exc:
        logger.warning("vm_console_observation_push_failed", capture_id=capture_id, error=str(exc))

    async with db_session_factory() as session:
        await store.update_capture_status(
            session, capture_id, "completed", vision_result=observation_payload, completed=True,
            wake_state=None,
        )

    # 7. 变量回写：按 produces path 从观察 Schema 提取 VM_CONSOLE_*。
    from app.tools.qkv.parser import parse_frontend_value
    from app.tools.qkv.signal import FrontendQueryType

    values = parse_frontend_value(
        FrontendQueryType.VM_CONSOLE, json.dumps(observation_payload), produces=produces
    )
    return VmConsoleCaptureResult(
        success=True,
        values=values,
        capture_id=capture_id,
        exec_id=run_exec_id,
        resolution={
            "near_black": near_black,
            "wake_executed": wake_executed,
            "artifact_id": artifact_id,
            "observation_status": observation.observation_status,
        },
    )


async def _fetch_artifact_bytes(http_client: InternalHTTPClient, artifact_id: str) -> bytes | None:
    """从 conversation-service 读取制品原图字节（内部鉴权；失败返回 None）。"""

    if not artifact_id:
        return None
    try:
        resp = await http_client.get(f"/internal/vm-console/artifacts/{artifact_id}/bytes")
        if resp.status_code != 200:
            return None
        return resp.content
    except Exception as exc:
        logger.warning("vm_console_artifact_fetch_failed", artifact_id=artifact_id, error=str(exc))
        return None


async def _request_wake_confirmation(
    http_client: InternalHTTPClient,
    *,
    db_session_factory: Any,
    capture_id: str,
    case_id: str,
    conversation_id: str,
    host_node_id: str,
    vm_id: str,
    user_id: str | None,
) -> bool:
    """发起唤醒确认卡并等待用户决定；超时/拒绝返回 False（继续基线识图）。"""

    wake_token = uuid.uuid4().hex
    wake_token_hash = hashlib.sha256(wake_token.encode()).hexdigest()

    async with db_session_factory() as session:
        await store.update_capture_status(
            session, capture_id, "wake_confirmation_pending",
            wake_state="confirmation_pending", wake_token_hash=wake_token_hash,
        )

    try:
        resp = await http_client.post(
            f"/internal/conversations/{conversation_id}/vm-console-wake-confirm",
            json={
                "capture_id": capture_id,
                "case_id": case_id,
                "host_node_id": host_node_id,
                "vm_id": vm_id,
                "wake_token": wake_token,
                "timeout_seconds": WAKE_CONFIRM_TIMEOUT_SECONDS,
            },
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("vm_console_wake_confirm_push_failed", capture_id=capture_id, error=str(exc))
        return False

    async with db_session_factory() as session:
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="wake_confirm_requested", case_id=case_id,
            conversation_id=conversation_id, actor="system:adapter",
            detail={"timeout_seconds": WAKE_CONFIRM_TIMEOUT_SECONDS},
        )

    redis = _redis_client()
    decision_key = f"vm_console_wake_decision:{capture_id}"
    raw = await redis.blpop(decision_key, timeout=WAKE_CONFIRM_TIMEOUT_SECONDS)
    if raw is None:
        async with db_session_factory() as session:
            await store.update_capture_status(
                session, capture_id, "quality_checked", wake_state="timed_out", clear_wake_token=True
            )
        VM_CONSOLE_WAKE_TOTAL.labels(decision="timed_out", result="not_attempted", mode="online").inc()
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="wake_timed_out", case_id=case_id,
            conversation_id=conversation_id, actor="system:adapter",
        )
        return False
    try:
        decision = json.loads(raw[1])
    except (json.JSONDecodeError, IndexError):
        return False
    confirmed = bool(decision.get("confirmed"))
    async with db_session_factory() as session:
        if not confirmed:
            await store.update_capture_status(
                session, capture_id, "quality_checked", wake_state="declined", clear_wake_token=True
            )
            VM_CONSOLE_WAKE_TOTAL.labels(decision="declined", result="not_attempted", mode="online").inc()
            await store.insert_audit_event(
                session, capture_id=capture_id, event_type="wake_declined", case_id=case_id,
                conversation_id=conversation_id, actor="user:interactive",
            )
    return confirmed


async def run_wake_and_recapture(
    http_client: InternalHTTPClient,
    *,
    db_session_factory: Any,
    capture_id: str,
    conversation_id: str,
    case_id: str,
    host_node_id: str,
    vm_id: str,
    node_ip: str | None,
    timeout_seconds: int,
    intent: VmConsoleCaptureIntent,
    trace_id: str,
) -> dict[str, Any]:
    """已确认后的固定唤醒 + 稳定窗口 + 重截图（一次性，不循环重试）。"""

    import asyncio

    from shared.resolution.vm_console import build_wake_intent

    wake_intent = build_wake_intent(
        host_node_id, vm_id, timeout_seconds=timeout_seconds, catalog_revision=intent.catalog_revision
    )
    wake_exec_id = f"vmc-wake-{uuid.uuid4().hex[:12]}"

    async with db_session_factory() as session:
        await store.update_capture_status(session, capture_id, "waking")
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="waking", case_id=case_id,
            conversation_id=conversation_id, actor="system:bridge", trace_id=trace_id,
        )
    wake_result = await _push_bridge_op(
        http_client,
        conversation_id=conversation_id,
        capture_id=capture_id,
        exec_id=wake_exec_id,
        operation="wake_down_key",
        host_node_id=host_node_id,
        vm_id=vm_id,
        node_ip=node_ip,
        case_id=case_id,
        timeout_seconds=min(timeout_seconds, 15),
        intent=wake_intent,
        role="wake",
        trace_id=trace_id,
    )
    if wake_result.get("failed") or wake_result.get("error_type"):
        async with db_session_factory() as session:
            await store.update_capture_status(
                session, capture_id, "quality_checked",
                error_code="WAKE_FAILED", error_summary=str(wake_result.get("error") or "唤醒操作失败"),
            )
        return {"success": False, "error": wake_result.get("error")}

    async with db_session_factory() as session:
        await store.update_capture_status(session, capture_id, "recapturing")
    await asyncio.sleep(WAKE_SETTLE_SECONDS)

    recapture_exec_id = f"vmc-recap-{uuid.uuid4().hex[:12]}"
    async with db_session_factory() as session:
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="wake_confirmed", case_id=case_id,
            conversation_id=conversation_id, actor="user:interactive", trace_id=trace_id,
        )
    recapture = await _push_bridge_op(
        http_client,
        conversation_id=conversation_id,
        capture_id=capture_id,
        exec_id=recapture_exec_id,
        operation="capture_baseline",
        host_node_id=host_node_id,
        vm_id=vm_id,
        node_ip=node_ip,
        case_id=case_id,
        timeout_seconds=timeout_seconds,
        intent=intent,
        role="recapture",
        trace_id=trace_id,
    )
    if recapture.get("failed") or recapture.get("error_type"):
        async with db_session_factory() as session:
            await store.update_capture_status(
                session, capture_id, "quality_checked",
                error_code="BASELINE_CAPTURE_FAILED",
                error_summary=str(recapture.get("error") or "重截图失败"),
            )
        return {"success": False, "error": recapture.get("error")}

    recapture_artifact = str(recapture.get("artifact_id") or "")
    async with db_session_factory() as session:
        await store.insert_audit_event(
            session, capture_id=capture_id, event_type="recaptured", case_id=case_id,
            conversation_id=conversation_id, actor="system:bridge", trace_id=trace_id,
            detail={"artifact_sha256": str(recapture.get("sha256") or "")},
        )
    async with db_session_factory() as session:
        await store.update_capture_status(
            session, capture_id, "quality_checked",
            recapture_artifact_id=recapture_artifact or None,
            effective_artifact_id=recapture_artifact or None,
            quality_metrics=recapture.get("quality") or {},
        )
    return {"success": True, "artifact_id": recapture_artifact, "quality": recapture.get("quality") or {}}
