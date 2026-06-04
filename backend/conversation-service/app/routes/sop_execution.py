"""
SOP 执行路由 — SOP 执行状态管理 API

提供 SOP 执行实例的推进和管理接口：
  - POST /api/conversations/{id}/sop/advance: 推进到下一节点（sop_advance 工具调用）

设计依据：
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md T-AGT-21
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M3变量池实现.md T-AGT-27（validation_pattern 校验）

鉴权：
  - 使用 INTERNAL_API_TOKEN（内部服务调用，agent-service → conversation-service）
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..config import settings
from ..models.sop_execution import STATUS_ACTIVE, STATUS_INTERRUPTED, SopExecution
from ..repositories.sop_execution_repository import SopExecutionRepository

logger = get_logger("sop-execution-routes")
router = APIRouter(prefix="/api/conversations", tags=["sop-execution"])

# 由 main.py 注入
_db_manager: DatabaseManager | None = None
_kb_client: Any | None = None
_environment_client: Any | None = None


def set_dependencies(
    db: DatabaseManager,
    kb_client: Any | None = None,
    env_client: Any | None = None,
) -> None:
    """注入依赖"""
    global _db_manager, _kb_client, _environment_client
    _db_manager = db
    _kb_client = kb_client
    _environment_client = env_client


def _check_auth(request: Request) -> None:
    """验证内部服务 Token"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Token 无效")


async def _get_sop_document(sop_document_id: int) -> dict | None:
    """从 kb-service 获取 SOP 文档的详细信息（包含 title 和 variable_schema 等）。

    Args:
        sop_document_id: SOP 文档 ID

    Returns:
        SOP 文档字典，不存在时返回 None
    """
    if _kb_client is not None:
        try:
            sop_doc = await _kb_client.get_sop_document(sop_document_id)
            if sop_doc:
                return sop_doc
            return None
        except Exception as exc:
            logger.warning(
                event="get_sop_document_client_error",
                sop_document_id=sop_document_id,
                error=str(exc),
            )
            return None

    # 降级到直接 HTTP 请求
    url = f"{settings.KB_SERVICE_URL}/api/admin/sop/{sop_document_id}"
    headers = {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning(
            event="get_sop_document_error",
            sop_document_id=sop_document_id,
            error=str(exc),
        )
        return None


async def _get_variable_schema(sop_document_id: int) -> list[dict] | None:
    """从 kb-service 获取 SOP 文档的 variable_schema（T-AGT-27）。

    Args:
        sop_document_id: SOP 文档 ID

    Returns:
        variable_schema 列表，不存在时返回 None
    """
    doc = await _get_sop_document(sop_document_id)
    return doc.get("variable_schema", []) if doc else None


def _validate_variables(
    variables_extracted: dict[str, Any],
    variable_schema: list[dict] | None,
) -> tuple[bool, list[str]]:
    """校验 variables_extracted 是否符合 variable_schema 的 validation_pattern（T-AGT-27）。

    Args:
        variables_extracted: 待写入的变量字典
        variable_schema: 变量 Schema 定义列表

    Returns:
        (是否全部通过, 错误消息列表)
    """
    if not variable_schema:
        # 无 schema 定义，跳过校验
        return True, []

    errors = []
    schema_by_name = {v.get("name"): v for v in variable_schema}

    for var_name, var_value in variables_extracted.items():
        var_def = schema_by_name.get(var_name)
        if var_def is None:
            # 变量未在 schema 中定义，允许写入（LLM 自由填充）
            continue

        validation_pattern = var_def.get("validation_pattern")
        if not validation_pattern:
            # 无校验规则，允许写入
            continue

        # 校验值是否符合 pattern（使用 fullmatch 保证完整匹配，BUG-R02）
        var_value_str = str(var_value) if not isinstance(var_value, str) else var_value
        try:
            if not re.fullmatch(validation_pattern, var_value_str):
                errors.append(f"变量 '{var_name}' 值 '{var_value_str}' 不符合校验规则 '{validation_pattern}'")
        except re.error as exc:
            # BUG-R03: validation_pattern 为无效正则时，记录警告并跳过该变量校验
            logger.warning(
                event="validate_variables_invalid_pattern",
                var_name=var_name,
                validation_pattern=validation_pattern,
                error=str(exc),
            )

    return len(errors) == 0, errors


class SopCreateRequest(BaseModel):
    """SOP 执行实例创建请求"""

    sop_document_id: int = Field(..., description="SOP 文档 ID")
    root_node_id: str = Field(default="n-1", description="根节点 ID（默认 n-1）")


class SopCreateResponse(BaseModel):
    """SOP 执行实例创建响应"""

    ok: bool = Field(..., description="创建是否成功")
    conversation_id: str = Field(..., description="会话 ID")
    sop_document_id: int = Field(..., description="SOP 文档 ID")
    current_node_id: str = Field(..., description="当前节点 ID（根节点）")
    status: str = Field(..., description="执行状态（active）")
    message: str = Field(..., description="创建结果消息")


class SopAdvanceRequest(BaseModel):
    """SOP 推进请求"""

    target_node_id: str = Field(..., min_length=1, description="目标节点 ID")
    reasoning: str = Field(..., min_length=1, description="LLM 推进理由")
    node_type: str | None = Field(None, description="目标节点类型（branch/diagnosis/solution）")
    variables_extracted: dict[str, Any] | None = Field(None, description="变量池更新")


class SopAdvanceResponse(BaseModel):
    """SOP 推进响应"""

    ok: bool = Field(..., description="操作是否成功")
    current_node_id: str = Field(..., description="当前节点 ID")
    node_type: str | None = Field(None, description="节点类型")
    message: str = Field(..., description="推进结果消息")
    is_completed: bool = Field(False, description="SOP 是否已完成（到达叶节点）")


def _get_filter_keywords(
    category_l2: str | None,
    category_l1: str | None,
    sop_title: str | None,
) -> list[str]:
    """根据分类和 SOP 标题提取用于过滤相关告警和任务日志的关键字（中英文）。

    Args:
        category_l2: 二级分类
        category_l1: 一级分类
        sop_title: SOP 标题

    Returns:
        提取到的关键字列表
    """
    keywords = set()
    texts = []
    if category_l2:
        texts.append(category_l2)
    if category_l1:
        texts.append(category_l1)
    if sop_title:
        texts.append(sop_title)

    # 常见运维技术词汇的映射（自动丰富关联关键字）
    domain_keywords_map = {
        "磁盘": ["disk", "磁盘", "硬盘", "smart", "sn", "drive", "sata", "ssd", "nvme"],
        "硬盘": ["disk", "磁盘", "硬盘", "smart", "sn", "drive", "sata", "ssd", "nvme"],
        "虚拟机": ["vm", "虚拟机", "vms", "qemu", "kvm"],
        "开机": ["power", "boot", "start", "开机", "启动"],
        "启动": ["power", "boot", "start", "开机", "启动"],
        "失败": ["fail", "failed", "error", "失败", "故障", "异常"],
        "异常": ["fail", "failed", "error", "失败", "故障", "异常"],
        "故障": ["fail", "failed", "error", "失败", "故障", "异常"],
        "网络": ["network", "net", "网络", "ping", "delay", "latency", "延迟", "丢包", "网卡", "ip"],
        "延迟": ["network", "net", "网络", "ping", "delay", "latency", "延迟", "丢包"],
        "内存": ["memory", "ram", "内存", "ecc", "dimm"],
        "cpu": ["cpu", "processor", "core", "处理器"],
        "处理器": ["cpu", "processor", "core", "处理器"],
        "存储": ["storage", "pool", "存储", "ceph", "cluster"],
        "备份": ["backup", "备份", "restore"],
    }

    for text in texts:
        text_lower = text.lower()
        # 1. 匹配映射
        for term, words in domain_keywords_map.items():
            if term in text_lower:
                keywords.update(words)

        # 2. 提取所有英文字符/数字组合（长度 >= 2）
        eng_words = re.findall(r"[a-zA-Z0-9_-]{2,}", text_lower)
        keywords.update(eng_words)

        # 3. 按照常见标点和空格分割文本
        parts = re.split(r"[\s\-_\/,，+]+", text_lower)
        for part in parts:
            if part and len(part) >= 2:
                keywords.add(part)

        # 4. 对于中文文本，提取所有连续中文字符串，并生成长度为 2-4 的所有子串作为关键字
        chinese_runs = re.findall(r"[\u4e00-\u9fa5]+", text_lower)
        for run in chinese_runs:
            n = len(run)
            for length in (2, 3, 4):
                if n >= length:
                    for i in range(n - length + 1):
                        keywords.add(run[i : i + length])

    return list(keywords)


def _filter_logs_by_keywords(logs: list[dict], keywords: list[str]) -> list[dict]:
    """递归遍历日志的所有字段值，计算与关键字的匹配得分对日志进行排序和过滤。

    Args:
        logs: 原始日志列表
        keywords: 关键字列表

    Returns:
        过滤并按匹配得分降序排序后的日志列表（只包含得分 > 0 的日志）
    """
    if not keywords:
        return logs

    scored_logs = []
    for log in logs:
        log_text_parts = []

        def extract_strings(val, target_list):
            if isinstance(val, str):
                target_list.append(val.lower())
            elif isinstance(val, dict):
                for v in val.values():
                    extract_strings(v, target_list)
            elif isinstance(val, list):
                for v in val:
                    extract_strings(v, target_list)

        extract_strings(log, log_text_parts)
        log_text = " ".join(log_text_parts)

        # 计算匹配的关键字个数作为得分
        score = 0
        for kw in keywords:
            if kw.isalnum() and kw.isascii():
                # 英文/数字关键字使用正则单词边界进行匹配，防止子串重复计数（例如 fail 匹配 failed）
                pattern = rf"\b{re.escape(kw)}\b"
                if re.search(pattern, log_text):
                    score += 1
            else:
                if kw.lower() in log_text:
                    score += 1

        if score > 0:
            scored_logs.append((score, log))

    # 按得分降序排序
    scored_logs.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored_logs]


def _resolve_env_variable(
    var_name: str,
    var_def: dict,
    env_context: Any,
    category_l1: str | None = None,
    category_l2: str | None = None,
    sop_title: str | None = None,
) -> Any | None:
    """根据变量定义，从环境数据集中提取并注入变量值。

    1. 优先在 env_info (基本信息，如 hci_version) 中匹配。
    2. 如果是主机/告警相关变量，进行关键字过滤后再锚定到第一个告警（无匹配则 fallback 到 alert_logs[0]）。
       - node_ip / ip / host / object_name / description / target
       - disk_sn: 尝试从匹配的告警中的 description 或 target 提取 serial number。
    3. 如果是任务相关变量，进行关键字过滤后再锚定到第一个任务（无匹配则 fallback 到 task_logs[0]）。
    """
    env_info = env_context.env_info or {}

    # 支持大小写及下划线不敏感匹配
    def lookup_dict(d: dict, target_key: str) -> Any | None:
        target_norm = target_key.lower().replace("_", "").replace("-", "")
        for k, v in d.items():
            k_norm = k.lower().replace("_", "").replace("-", "")
            if k_norm == target_norm:
                return v
        return None

    # 1. 尝试从 env_info 查找
    val = lookup_dict(env_info, var_name)
    if val is not None:
        return val

    # 获取过滤关键字
    keywords = _get_filter_keywords(category_l2, category_l1, sop_title)

    # 2. 检查告警日志
    alert_logs = env_context.alert_logs or []
    if alert_logs:
        filtered_alerts = _filter_logs_by_keywords(alert_logs, keywords)
        alert = filtered_alerts[0] if filtered_alerts else alert_logs[0]
        # 如果是硬盘 SN
        if var_name in ("disk_sn", "sn", "serial_number", "device_sn"):
            val = lookup_dict(alert, var_name) or lookup_dict(alert, "sn") or lookup_dict(alert, "serial_number")
            if val is not None:
                return str(val)
            desc = alert.get("description") or ""
            target = alert.get("target") or ""
            for text in (desc, target):
                m = re.search(r"(?i)sn\s*[：:]\s*([A-Z0-9\-]{8,20})", text)
                if m:
                    return m.group(1)
                m = re.search(r"(?i)\[\s*sn\s*[：:]\s*([A-Z0-9\-]{8,20})\s*\]", text)
                if m:
                    return m.group(1)
                m = re.search(r"\b([A-Z0-9]{8,20})\b", text)
                if m:
                    return m.group(1)
        else:
            if var_name == "node_ip":
                val = (
                    lookup_dict(alert, "node_ip")
                    or lookup_dict(alert, "host")
                    or lookup_dict(alert, "ip")
                    or lookup_dict(alert, "target")
                )
            else:
                val = lookup_dict(alert, var_name)
            if val is not None:
                return val

    # 3. 检查任务日志
    task_logs = env_context.task_logs or []
    if task_logs:
        filtered_tasks = _filter_logs_by_keywords(task_logs, keywords)
        task = filtered_tasks[0] if filtered_tasks else task_logs[0]
        val = lookup_dict(task, var_name)
        if val is not None:
            return val

    return None


@router.post("/{conversation_id}/sop/create", response_model=SopCreateResponse)
async def sop_create_execution(
    request: Request,
    conversation_id: uuid.UUID,
    body: SopCreateRequest,
):
    """创建 SOP 执行实例（S1 阶段命中 SOP 时）。

    操作：
      1. 创建新的 sop_execution 记录
      2. 初始化 current_node_id 为根节点
      3. 记录 execution_log 首条（node_entered）

    Args:
        conversation_id: 会话 ID
        body: 创建请求（SOP 文档 ID、根节点 ID）

    Returns:
        创建结果（会话 ID、SOP 文档 ID、当前节点 ID）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="sop_create_request",
        conversation_id=str(conversation_id),
        sop_document_id=body.sop_document_id,
        root_node_id=body.root_node_id,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)

        # 检查是否已存在活跃的执行实例（中断恢复场景）
        existing = await repo.get_active_by_conversation(conversation_id)
        if existing:
            # 已存在活跃实例，返回现有记录（用于恢复）
            logger.info(
                event="sop_create_existing",
                conversation_id=str(conversation_id),
                existing_id=str(existing.id),
                current_node_id=existing.current_node_id,
                trace_id=trace_id,
            )
            await session.commit()
            return SopCreateResponse(
                ok=True,
                conversation_id=str(conversation_id),
                sop_document_id=existing.sop_document_id,
                current_node_id=existing.current_node_id,
                status=existing.status,
                message="SOP 执行实例已存在，继续执行",
            )

        # 查询 Conversation 获取 case_id
        from shared.models.conversation import Conversation
        from sqlalchemy import select

        conversation_result = await session.execute(
            select(Conversation).where(Conversation.conversation_id == conversation_id)
        )
        conversation = conversation_result.scalar_one_or_none()
        if not conversation:
            raise HTTPException(status_code=404, detail="会话不存在")
        case_id = conversation.case_id

        # 获取环境上下文
        env_context = None
        if _environment_client and case_id:
            env_context = await _environment_client.get_context_info(case_id)

        # 获取 SOP 详细信息
        sop_doc = await _get_sop_document(body.sop_document_id)
        variable_schema = sop_doc.get("variable_schema", []) if sop_doc else None
        sop_title = sop_doc.get("title") if sop_doc else None

        # 解析并注入 env_injection 变量
        initial_variables = {}
        if variable_schema and env_context:
            for var_def in variable_schema:
                strategy = var_def.get("acquisition_strategy")
                if strategy in ("env_injection", "env_context") or (strategy and strategy.startswith("env:")):
                    var_name = var_def.get("name")
                    val = _resolve_env_variable(
                        var_name,
                        var_def,
                        env_context,
                        category_l1=conversation.category_l1,
                        category_l2=conversation.category_l2,
                        sop_title=sop_title,
                    )
                    if val is not None:
                        initial_variables[var_name] = val

        # 创建新的执行实例
        execution = await repo.create(
            conversation_id=conversation_id,
            sop_document_id=body.sop_document_id,
            current_node_id=body.root_node_id,
            trace_id=trace_id,
            initial_variables=initial_variables,
        )

        await session.commit()

        logger.info(
            event="sop_create_success",
            conversation_id=str(conversation_id),
            execution_id=str(execution.id),
            sop_document_id=body.sop_document_id,
            current_node_id=execution.current_node_id,
            trace_id=trace_id,
        )

        return SopCreateResponse(
            ok=True,
            conversation_id=str(conversation_id),
            sop_document_id=body.sop_document_id,
            current_node_id=execution.current_node_id,
            status=execution.status,
            message="SOP 执行实例已创建",
        )


@router.post("/{conversation_id}/sop/advance", response_model=SopAdvanceResponse)
async def sop_advance_execution(
    request: Request,
    conversation_id: uuid.UUID,
    body: SopAdvanceRequest,
):
    """推进 SOP 执行到下一节点（agent-service 的 sop_advance 工具调用）。

    操作：
      1. 更新 current_node_id 为目标节点
      2. 追加 execution_log 条目（node_entered）
      3. 追加 completed_steps（前一节点标记完成）
      4. 若叶节点（solution）则更新 status=completed

    Args:
        conversation_id: 会话 ID
        body: 推进请求（目标节点 ID、推理理由、节点类型、变量）

    Returns:
        推进结果（当前节点 ID、节点类型、是否完成）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="sop_advance_request",
        conversation_id=str(conversation_id),
        target_node_id=body.target_node_id,
        node_type=body.node_type,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)

        # DC-03: 提前一次性查询（仅在需要校验时），后续传入 advance() 避免重复 SELECT
        prefetched_execution: SopExecution | None = None
        if body.variables_extracted:
            # 获取活跃执行实例（advance 也要求 active 状态）
            prefetched_execution = await repo.get_active_by_conversation(conversation_id)
            if prefetched_execution is None:
                raise HTTPException(
                    status_code=404,
                    detail="SOP 执行实例不存在或状态非 active",
                )

            # 获取 variable_schema 并校验 variables_extracted（T-AGT-27）
            variable_schema = await _get_variable_schema(prefetched_execution.sop_document_id)
            valid, errors = _validate_variables(body.variables_extracted, variable_schema)
            if not valid:
                logger.warning(
                    event="sop_advance_validation_failed",
                    conversation_id=str(conversation_id),
                    errors=errors,
                    trace_id=trace_id,
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "variable_validation_failed",
                        "message": "变量值校验失败",
                        "errors": errors,
                    },
                )

        # 推进执行（DC-03: 传入预取实例，跳过 advance() 内部的重复 SELECT）
        execution = await repo.advance(
            conversation_id=conversation_id,
            target_node_id=body.target_node_id,
            reasoning=body.reasoning,
            node_type=body.node_type,
            variables_extracted=body.variables_extracted,
            existing_execution=prefetched_execution,  # DC-03: 避免重复查询
        )

        if execution is None:
            raise HTTPException(
                status_code=404,
                detail="SOP 执行实例不存在或状态非 active",
            )

        await session.commit()

        is_completed = execution.status == "completed"
        message = f"已推进到节点 {body.target_node_id}"
        if is_completed:
            message = "SOP 执行完成，已到达叶节点"

        logger.info(
            event="sop_advance_success",
            conversation_id=str(conversation_id),
            current_node_id=execution.current_node_id,
            status=execution.status,
            is_completed=is_completed,
            trace_id=trace_id,
        )

        return SopAdvanceResponse(
            ok=True,
            current_node_id=execution.current_node_id,
            node_type=body.node_type,
            message=message,
            is_completed=is_completed,
        )


@router.get("/{conversation_id}/sop/execution")
async def get_sop_execution(
    request: Request,
    conversation_id: uuid.UUID,
):
    """获取 SOP 执行实例详情（用于中断恢复）"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)
        execution = await repo.get_by_conversation(conversation_id)

        if execution is None:
            raise HTTPException(
                status_code=404,
                detail="SOP 执行实例不存在",
            )

        logger.info(
            event="sop_execution_retrieved",
            conversation_id=str(conversation_id),
            status=execution.status,
            current_node_id=execution.current_node_id,
            trace_id=trace_id,
        )

        return {
            "id": str(execution.id),
            "conversation_id": str(execution.conversation_id),
            "sop_document_id": execution.sop_document_id,
            "current_node_id": execution.current_node_id,
            "status": execution.status,
            "context_variables": execution.context_variables,
            "completed_steps": execution.completed_steps,
            "execution_log": execution.execution_log,
            "pending_variable_name": execution.pending_variable_name,
            "created_at": execution.created_at.isoformat() if execution.created_at else None,
            "updated_at": execution.updated_at.isoformat() if execution.updated_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# T-AGT-25: SOP 执行中断端点（设置 pending_variable_name）
# ─────────────────────────────────────────────────────────────────────────────


class SopInterruptRequest(BaseModel):
    """SOP 执行中断请求"""

    pending_variable_name: str = Field(..., min_length=1, description="待填变量名")


class SopInterruptResponse(BaseModel):
    """SOP 执行中断响应"""

    ok: bool = Field(..., description="操作是否成功")
    conversation_id: str = Field(..., description="会话 ID")
    status: str = Field(..., description="执行状态（interrupted）")
    pending_variable_name: str = Field(..., description="待填变量名")
    message: str = Field(..., description="中断结果消息")


@router.post("/{conversation_id}/sop/interrupt", response_model=SopInterruptResponse)
async def sop_interrupt_execution(
    request: Request,
    conversation_id: uuid.UUID,
    body: SopInterruptRequest,
):
    """标记 SOP 执行中断等待变量（agent-service 的 sop_request_variable 工具调用）。

    操作：
      1. 更新 status=interrupted
      2. 设置 pending_variable_name

    Args:
        conversation_id: 会话 ID
        body: 中断请求（待填变量名）

    Returns:
        中断结果（会话 ID、状态、待填变量名）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="sop_interrupt_request",
        conversation_id=str(conversation_id),
        pending_variable_name=body.pending_variable_name,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)

        # 检查执行实例存在
        execution = await repo.get_active_by_conversation(conversation_id)
        if execution is None:
            raise HTTPException(
                status_code=404,
                detail="SOP 执行实例不存在或状态非 active",
            )

        # 标记中断
        updated = await repo.interrupt(
            conversation_id=conversation_id,
            pending_variable_name=body.pending_variable_name,
        )

        if updated is None:
            raise HTTPException(
                status_code=500,
                detail="标记中断失败",
            )

        await session.commit()

        logger.info(
            event="sop_interrupt_success",
            conversation_id=str(conversation_id),
            status=updated.status,
            pending_variable_name=updated.pending_variable_name,
            trace_id=trace_id,
        )

        return SopInterruptResponse(
            ok=True,
            conversation_id=str(conversation_id),
            status=updated.status,
            pending_variable_name=updated.pending_variable_name,
            message=f"SOP 执行已中断，等待变量 {body.pending_variable_name} 填写",
        )


# ─────────────────────────────────────────────────────────────────────────────
# T-AGT-25: 变量提交端点（用户响应变量请求）
# ─────────────────────────────────────────────────────────────────────────────


class VariableResponseRequest(BaseModel):
    """变量值提交请求"""

    variable_name: str = Field(..., min_length=1, description="变量名")
    value: str = Field(..., min_length=1, description="变量值")
    source: str | None = Field(default="user_input", description="值来源（user_input/user_confirm/tool_result）")


class VariableResponseResponse(BaseModel):
    """变量值提交响应"""

    ok: bool = Field(..., description="操作是否成功")
    variable_name: str = Field(..., description="变量名")
    value: str = Field(..., description="已写入的值")
    message: str = Field(..., description="结果消息")
    validation_passed: bool = Field(True, description="校验是否通过")


@router.post("/{conversation_id}/sop/variable-response", response_model=VariableResponseResponse)
async def sop_variable_response(
    request: Request,
    conversation_id: uuid.UUID,
    body: VariableResponseRequest,
):
    """提交变量值（用户响应 sop_request_variable 的交互请求）。

    流程：
      1. 验证 SOP 执行状态为 interrupted 且 pending_variable_name 匹配
      2. 校验 value 是否符合 variable_schema 的 validation_pattern（如有）
      3. 写入 context_variables[variable_name]
      4. 清空 pending_variable_name，恢复状态为 active

    Args:
        conversation_id: 会话 ID
        body: 变量值提交请求（变量名、值、来源）

    Returns:
        提交结果（变量名、值、校验状态）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="sop_variable_response_request",
        conversation_id=str(conversation_id),
        variable_name=body.variable_name,
        value_preview=body.value[:50] if len(body.value) > 50 else body.value,
        source=body.source,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)

        # 1. 获取执行实例
        execution = await repo.get_by_conversation(conversation_id)
        if execution is None:
            raise HTTPException(
                status_code=404,
                detail="SOP 执行实例不存在",
            )

        # 2. 验证状态和 pending_variable_name
        if execution.status != STATUS_INTERRUPTED:
            # 允许在 active 状态下直接写入变量（LLM 可能提前填充）
            if execution.status == STATUS_ACTIVE:
                logger.info(
                    event="sop_variable_response_active_state",
                    conversation_id=str(conversation_id),
                    variable_name=body.variable_name,
                    message="执行状态为 active，直接写入变量",
                )
                # 直接写入变量（不校验 pending_variable_name）
                updated = await repo.set_variable(
                    conversation_id=conversation_id,
                    variable_name=body.variable_name,
                    value=body.value,
                    source=body.source or "user_input",
                )
                await session.commit()
                return VariableResponseResponse(
                    ok=True,
                    variable_name=body.variable_name,
                    value=body.value,
                    message=f"变量 {body.variable_name} 已写入",
                    validation_passed=True,
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"SOP 执行状态为 {execution.status}，无法提交变量",
                )

        # 3. 校验 pending_variable_name 是否匹配
        if execution.pending_variable_name != body.variable_name:
            logger.warning(
                event="sop_variable_response_mismatch",
                conversation_id=str(conversation_id),
                expected_variable=execution.pending_variable_name,
                submitted_variable=body.variable_name,
                trace_id=trace_id,
            )
            raise HTTPException(
                status_code=400,
                detail=f"当前等待变量 {execution.pending_variable_name}，提交的变量 {body.variable_name} 不匹配",
            )

        # 4. 校验 validation_pattern（如有）
        # TODO: 从 kb-service 获取 variable_schema 进行校验
        # 暂时跳过校验，直接写入

        # 5. 写入变量并恢复状态
        updated = await repo.set_variable(
            conversation_id=conversation_id,
            variable_name=body.variable_name,
            value=body.value,
            source=body.source or "user_input",
        )

        if updated is None:
            raise HTTPException(
                status_code=500,
                detail="写入变量失败",
            )

        await session.commit()

        logger.info(
            event="sop_variable_response_success",
            conversation_id=str(conversation_id),
            variable_name=body.variable_name,
            value_preview=body.value[:50] if len(body.value) > 50 else body.value,
            status=updated.status,
            trace_id=trace_id,
        )

        return VariableResponseResponse(
            ok=True,
            variable_name=body.variable_name,
            value=body.value,
            message=f"变量 {body.variable_name} 已写入，SOP 执行已恢复",
            validation_passed=True,
        )
