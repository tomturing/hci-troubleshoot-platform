"""
terminal_bridge 日志回采接口 - 统一工单关联的日志落库

提供前端（Custom-UI）在收到 terminal_bridge 经 WebSocket 推送的 bridge_log 结构化日志后，
批量回采到 conversation-service 落库的接口：
  - POST /api/bridge-logs: 批量回采 terminal_bridge 执行日志（前端 -> conversation-service）

设计依据：
  - docs/solution/events/2026-07-20-terminal-bridge可观测性与日志回采重设计.md
  - docs/solution/events/2026-07-20-terminal-bridge回采链路断裂根因分析.md
  - OBS-TERMINAL-BRIDGE-001

鉴权（SRC L2 加固，2026-10-08）：
  - customer 前端经 api-gateway：网关强制 require_user（服务端身份 Cookie）并注入
    HMAC 签名 X-Client-ID，本服务据此做工单归属校验（_verify_batch_ownership /
    _assert_case_owned），归属不符即 403；历史占位符 token 已彻底移除。
  - 集群内服务直连（agent-service 等）：Authorization: Bearer <INTERNAL_API_TOKEN>
    旁路归属校验。
  - 严格模式（STRICT_IDENTITY_SIGNATURE=true）无签名一律 401；过渡模式放行并匿名审计。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import text

from ..config import settings
from ..security.auth import authenticate_request

logger = get_logger("bridge-logs-routes")
router = APIRouter(tags=["bridge-logs"])

_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖（由 main.py 在 lifespan 中调用）"""
    global _db_manager
    _db_manager = db


# 鉴权模型（SRC L2 加固）：customer 前端经网关走签名 X-Client-ID 归属校验
# （_verify_batch_ownership / _assert_case_owned），彻底移除历史占位符 token；
# 集群内服务直连可用 Bearer <INTERNAL_API_TOKEN> 旁路归属校验。


def _parse_event_time(value: str | None) -> datetime | None:
    """把 Bridge RFC3339/RFC3339Nano 时间转换为 asyncpg 可绑定的 datetime。

    Go 默认输出纳秒精度的 RFC3339 时间，而 PostgreSQL/asyncpg 的 timestamptz
    参数要求 Python datetime。datetime.fromisoformat 会按数据库支持的微秒精度
    安全归一化多余的小数位，避免字符串在 prepared statement 绑定阶段被拒绝。

    Raises:
        ValueError: 时间格式非法或缺少时区。
    """
    if value is None:
        return None

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("event_time 必须包含时区")
    return parsed


def _is_internal_caller(authorization: str | None) -> bool:
    """内部服务直连判定：Authorization: Bearer <INTERNAL_API_TOKEN>。

    仅集群内服务（如 agent-service）直连本服务时使用；customer 前端经网关
    访问走签名 X-Client-ID 归属校验，不携带此令牌。
    """
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return authorization[7:].strip() == settings.INTERNAL_API_TOKEN


async def _assert_case_owned(session, case_id: str, client_id: str) -> None:
    """校验单个 case 归属 client；不符即 403（复用本服务参数化查询先例）。"""
    res = await session.execute(text('SELECT client_id FROM "case" WHERE case_id = :case_id'), {"case_id": case_id})
    row = res.fetchone()
    owner = row[0] if row else None
    if owner is None or owner != client_id:
        logger.warning(event="bridge_log_ownership_forbidden", case_id=str(case_id), client_id=client_id)
        raise HTTPException(status_code=403, detail="无权操作此工单的日志")


async def _verify_batch_ownership(body: BridgeLogBatch, client_id: str) -> None:
    """写库前校验 batch 内全部 case_id（含 fallback）归属当前 client；不符即 403。

    仅当网关已签名 X-Client-ID（authenticate_request 返回非 None）时执行；
    过渡模式（无签名）下跳过，与既有 customer 路由保持一致。
    """
    case_ids = {body.fallback_case_id}
    case_ids |= {entry.case_id for entry in body.logs}
    case_ids.discard(None)
    if not case_ids or _db_manager is None:
        return
    async for session in _db_manager.get_session():
        for case_id in case_ids:
            await _assert_case_owned(session, case_id, client_id)


async def _authenticate_batch(body: BridgeLogBatch, request: Request, authorization: str | None) -> str:
    """回采鉴权 + 工单归属校验（见模块头鉴权模型）。

    返回用于审计的 user_id："internal"（内部直连）| client_id（签名通过）|
    "anonymous"（过渡模式无签名）。
    """
    if _is_internal_caller(authorization):
        return "internal"
    client_id = await authenticate_request(request)
    if client_id is not None:
        await _verify_batch_ownership(body, client_id)
    return client_id or "anonymous"


class BridgeLogEntry(BaseModel):
    """单条 bridge_log 结构"""

    case_id: str | None = None
    trace_id: str | None = None
    custom_ui: str | None = None
    user_id: str | None = None
    node_ip: str | None = None
    level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARN|ERROR)$")
    event: str
    message: str
    extra: dict[str, Any] | None = None
    event_id: str | None = None
    bridge_instance_id: str | None = None
    seq: int | None = Field(default=None, ge=0)
    ts: str | None = None
    span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    trace_flags: str | None = Field(default=None, pattern=r"^[0-9a-f]{2}$")
    conversation_id: str | None = None
    exec_id: str | None = None
    tool_call_id: str | None = None
    service_name: str | None = Field(default=None, alias="service.name")
    service_version: str | None = Field(default=None, alias="service.version")
    deployment_environment: str | None = Field(default=None, alias="deployment.environment")

    model_config = {"populate_by_name": True}


class BridgeLogBatch(BaseModel):
    """批量 bridge_log"""

    logs: list[BridgeLogEntry]
    # 条目缺 case_id 时的兜底工单：由前端在回采时携带当前工单，
    # 避免无 case_id 的启动/连接类日志被静默丢弃（工单 Q2026092010235 教训）。
    fallback_case_id: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# 本地日志手动上传补采（自动回采不可用时的兜底通道）
# ─────────────────────────────────────────────────────────────────────────────

# 单文件内容上限（JSON 文本承载，含转义开销，取 12 MiB 保守值）
MAX_UPLOAD_FILE_BYTES = 12 * 1024 * 1024
# 单次上传文件数上限
MAX_UPLOAD_FILES = 5
# 单文件最大解析行数，防止超大文件拖垮请求
MAX_UPLOAD_LINES_PER_FILE = 200_000

# 落库结果 -> 统计字段名的映射（duplicates 为复数，避免调用方 KeyError）
_OUTCOME_STAT_KEYS = {
    "accepted": "accepted",
    "duplicate": "duplicates",
    "skipped": "skipped",
}


class BridgeLogUploadFile(BaseModel):
    """单个待上传的本地日志文件（JSONL 文本）"""

    name: str = Field(default="bridge.log", max_length=255)
    content: str


class BridgeLogUploadRequest(BaseModel):
    """手动上传补采请求"""

    # 上传时绑定的工单：条目自带 case_id 时以条目为准，缺失则继承该值
    case_id: str | None = None
    bridge_instance_id: str | None = None
    files: list[BridgeLogUploadFile] = Field(default_factory=list, max_length=MAX_UPLOAD_FILES)


async def _insert_entry(
    session,
    entry: BridgeLogEntry,
    user_id: str,
    fallback_case_id: str | None = None,
    upload_id: str | None = None,
) -> str:
    """落库单条 bridge 日志，返回 accepted / duplicate / skipped。

    Args:
        session: 数据库会话
        entry: 单条日志
        user_id: 审计用用户标识
        fallback_case_id: 条目缺 case_id 时的兜底工单
        upload_id: 手动上传批次 ID（非空时在 extra 中留痕，便于追溯来源）

    Returns:
        accepted（新写入）/ duplicate（被 event_id 去重）/ skipped（缺 case_id 或时间非法）
    """
    case_id = entry.case_id or fallback_case_id
    if not case_id:
        return "skipped"
    try:
        event_time = _parse_event_time(entry.ts)
    except (TypeError, ValueError):
        logger.warning(
            event="bridge_log_invalid_event_time",
            event_id=entry.event_id,
            bridge_instance_id=entry.bridge_instance_id,
            seq=entry.seq,
        )
        return "skipped"

    extra = entry.extra or {}
    if upload_id:
        extra = {**extra, "upload_id": upload_id, "source": "manual_upload"}
    extra_json = json.dumps(extra) if extra else None

    result = await session.execute(
        text(
            """
            INSERT INTO bridge_execution_logs
                (case_id, trace_id, custom_ui, user_id, node_ip, level, event, message, extra,
                 event_id, bridge_instance_id, seq, event_time, span_id, trace_flags,
                 conversation_id, exec_id, tool_call_id, service_name, service_version,
                 deployment_environment, command, command_sha256, exit_code, duration_ms,
                 stdout_len, stderr_len, output_preview, success, error_type, stdout_sha256,
                 stderr_sha256, stdout_truncated, stderr_truncated, artifact_id)
            VALUES
                (:case_id, :trace_id, :custom_ui, :user_id, :node_ip, :level, :event, :message,
                 CAST(:extra AS jsonb), CAST(:event_id AS uuid), :bridge_instance_id, :seq,
                 CAST(:event_time AS timestamptz), :span_id, :trace_flags,
                 CAST(:conversation_id AS uuid), :exec_id, :tool_call_id, :service_name,
                 :service_version, :deployment_environment, :command, :command_sha256,
                 :exit_code, :duration_ms, :stdout_len, :stderr_len, :output_preview,
                 :success, :error_type, :stdout_sha256, :stderr_sha256,
                 :stdout_truncated, :stderr_truncated, CAST(:artifact_id AS uuid))
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "case_id": case_id,
            "trace_id": entry.trace_id,
            "custom_ui": entry.custom_ui,
            "user_id": entry.user_id or user_id,
            "node_ip": entry.node_ip,
            "level": (entry.level or "INFO").upper(),
            "event": entry.event,
            "message": entry.message,
            "extra": extra_json,
            "event_id": entry.event_id,
            "bridge_instance_id": entry.bridge_instance_id,
            "seq": entry.seq,
            "event_time": event_time,
            "span_id": entry.span_id,
            "trace_flags": entry.trace_flags,
            "conversation_id": entry.conversation_id,
            "exec_id": entry.exec_id or extra.get("exec_id"),
            "tool_call_id": entry.tool_call_id,
            "service_name": entry.service_name or "terminal_bridge",
            "service_version": entry.service_version,
            "deployment_environment": entry.deployment_environment,
            "command": extra.get("command_redacted"),
            "command_sha256": extra.get("command_sha256"),
            "exit_code": extra.get("exit_code"),
            "duration_ms": extra.get("duration_ms"),
            "stdout_len": extra.get("stdout_len") or extra.get("stdout_bytes"),
            "stderr_len": extra.get("stderr_len") or extra.get("stderr_bytes"),
            "output_preview": None,
            "success": extra.get("success"),
            "error_type": extra.get("error_type"),
            "stdout_sha256": extra.get("stdout_sha256"),
            "stderr_sha256": extra.get("stderr_sha256"),
            "stdout_truncated": extra.get("stdout_truncated"),
            "stderr_truncated": extra.get("stderr_truncated"),
            "artifact_id": extra.get("artifact_id"),
        },
    )
    return "accepted" if result.rowcount else "duplicate"


def _coerce_upload_entry(raw: dict[str, Any]) -> BridgeLogEntry | None:
    """把一行 JSONL 宽松转换为 BridgeLogEntry，无法转换时返回 None（计入 invalid）。"""
    if not isinstance(raw, dict):
        return None
    event = str(raw.get("event") or "").strip()
    if not event:
        return None
    payload = dict(raw)
    payload["event"] = event
    payload["message"] = str(raw.get("message") or "")
    payload.setdefault("level", "INFO")
    try:
        return BridgeLogEntry.model_validate(payload)
    except Exception:
        return None


@router.post("/api/bridge-logs")
async def ingest_bridge_logs(
    body: BridgeLogBatch,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """批量回采 terminal_bridge 结构化执行日志（前端 → conversation-service）。

    所有条目必须携带 case_id（无 case_id 的日志在浏览器端已被过滤，此处再次校验），
    落库到 bridge_execution_logs，供端到端可观测性与工单复盘。

    鉴权与归属（SRC L2）：customer 前端经网关携带签名 X-Client-ID，写库前校验
    全部 case_id 归属当前 client（不符 403）；集群内服务直连可用
    Bearer <INTERNAL_API_TOKEN> 旁路归属校验。

    Args:
        body: 批量日志（logs）
        request: FastAPI 请求（读取网关签名的身份头）
        authorization: 内部服务令牌（可选，仅服务直连）

    Returns:
        ok / 接收条数 / 跳过条数
    """
    user_id = await _authenticate_batch(body, request, authorization)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    accepted = 0
    duplicates = 0
    skipped = 0
    async for session in _db_manager.get_session():
        for entry in body.logs:
            outcome = await _insert_entry(
                session,
                entry,
                user_id=user_id,
                fallback_case_id=body.fallback_case_id,
            )
            if outcome == "accepted":
                accepted += 1
            elif outcome == "duplicate":
                duplicates += 1
            else:
                skipped += 1
        await session.commit()

    logger.info(
        event="bridge_logs_ingested",
        user_id=user_id,
        accepted=accepted,
        skipped=skipped,
        duplicates=duplicates,
    )
    return {"ok": True, "accepted": accepted, "duplicates": duplicates, "skipped": skipped}


@router.post("/api/bridge-logs/upload")
async def upload_bridge_logs(
    body: BridgeLogUploadRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """手动上传本地 terminal_bridge 日志并补采落库（自动回采不可用时的兜底通道）。

    背景：自动回采链路（bridge → WebSocket → 浏览器 → 后端）强依赖浏览器在线，
    断网/页面关闭/回采失败时桥侧证据会整体丢失，导致诊断失败无法定因
    （工单 Q2026092010235）。本接口允许用户把 bridge 本地 JSONL 日志直接上传补采。

    工单归属：条目自带 case_id 时以条目为准；缺失则继承请求中的 case_id，
    实现"一个 bridge 服务多个工单"的场景下按工单归档。

    鉴权与归属（SRC L2）：customer 前端经网关携带签名 X-Client-ID，逐条校验有效
    case_id（条目自带或继承请求级）归属当前 client，不符即 403 整单拒绝（写库前
    校验、事务未提交 → 整体回滚不落库）；集群内服务直连可用 Bearer <INTERNAL_API_TOKEN> 旁路。

    Args:
        body: 上传请求（绑定工单 + 文件列表）
        request: FastAPI 请求（读取网关签名的身份头）
        authorization: 内部服务令牌（可选，仅服务直连）

    Returns:
        ok / upload_id / 各文件与总计的 accepted/duplicates/skipped/invalid 统计
    """
    if _is_internal_caller(authorization):
        user_id = "internal"
        enforce_ownership = False
        client_id: str | None = None
    else:
        client_id = await authenticate_request(request)
        enforce_ownership = client_id is not None
        user_id = client_id or "anonymous"

    # 功能开关：自动回采稳定后可整体屏蔽手动上传入口（Helm: bridgeLogs.uploadEnabled）
    if not getattr(settings, "BRIDGE_LOG_UPLOAD_ENABLED", True):
        raise HTTPException(status_code=404, detail="日志上传入口已关闭")

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    if not body.files:
        raise HTTPException(status_code=400, detail="未选择任何日志文件")

    upload_id = str(uuid.uuid4())
    files_summary: list[dict[str, Any]] = []
    total = {"accepted": 0, "duplicates": 0, "skipped": 0, "invalid": 0}

    owned_cache: set[str] = set()
    async for session in _db_manager.get_session():
        # 归属校验：请求级绑定工单先行校验（不符 403，事务未提交 → 整单回滚不落库）
        if enforce_ownership and body.case_id:
            await _assert_case_owned(session, body.case_id, client_id)
            owned_cache.add(body.case_id)
        for upload_file in body.files:
            raw_bytes = len(upload_file.content.encode("utf-8"))
            if raw_bytes > MAX_UPLOAD_FILE_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件 {upload_file.name} 超过 {MAX_UPLOAD_FILE_BYTES // 1024 // 1024} MiB 上限",
                )

            file_stats = {"accepted": 0, "duplicates": 0, "skipped": 0, "invalid": 0, "lines": 0}
            for line in upload_file.content.splitlines():
                file_stats["lines"] += 1
                if file_stats["lines"] > MAX_UPLOAD_LINES_PER_FILE:
                    file_stats["invalid"] += 1
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw_entry: Any = json.loads(stripped)
                except json.JSONDecodeError:
                    file_stats["invalid"] += 1
                    continue

                entry = _coerce_upload_entry(raw_entry if isinstance(raw_entry, dict) else {})
                if entry is None:
                    file_stats["invalid"] += 1
                    continue
                # 上传时未指定实例 ID 的条目，用请求级实例 ID 补全，保证后续去重可用
                if not entry.bridge_instance_id and body.bridge_instance_id:
                    entry.bridge_instance_id = body.bridge_instance_id

                # 归属校验：条目自带 case_id 可能指向他人工单，写库前惰性校验（按 case 去重）
                if enforce_ownership:
                    effective_case = entry.case_id or body.case_id
                    if effective_case and effective_case not in owned_cache:
                        await _assert_case_owned(session, effective_case, client_id)
                        owned_cache.add(effective_case)

                outcome = await _insert_entry(
                    session,
                    entry,
                    user_id=user_id,
                    fallback_case_id=body.case_id,
                    upload_id=upload_id,
                )
                # 落库结果（accepted/duplicate/skipped）映射到统计键（duplicates 为复数）
                file_stats[_OUTCOME_STAT_KEYS[outcome]] += 1

            for key in ("accepted", "duplicates", "skipped", "invalid"):
                total[key] += file_stats[key]
            files_summary.append(
                {
                    "name": upload_file.name,
                    "bytes": raw_bytes,
                    "sha256": hashlib.sha256(upload_file.content.encode("utf-8")).hexdigest(),
                    **file_stats,
                }
            )
        await session.commit()

    logger.info(
        event="bridge_logs_uploaded",
        user_id=user_id,
        upload_id=upload_id,
        case_id=body.case_id,
        files=len(body.files),
        **total,
    )
    return {
        "ok": True,
        "upload_id": upload_id,
        "case_id": body.case_id,
        **total,
        "files": files_summary,
    }
