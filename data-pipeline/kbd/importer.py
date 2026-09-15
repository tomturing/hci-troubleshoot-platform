"""
data-pipeline/kbd/importer.py — KBD 条目入库（API 调用版）

功能：
  从文件缓存（cache/{support_id}/raw.json）通过 converter 生成 content_md，
  然后调用 kb-service API `/api/kb/kbd/ingest` 写入 kbd_entry 表。

变更（T2-03）：
  - 不再直接写数据库（废弃 asyncpg 直接写入）
  - 改为调用 kb-service API `/api/kb/kbd/ingest`
  - API 端负责写入 kbd_entry 表，状态默认 draft
  - 幂等性由 API 端 support_id 唯一性校验保证

变更（自动 port-forward）：
  - 检测 kb-service 是否可达（k3s ClusterIP 服务本地无法直接访问）
  - 自动启动 kubectl port-forward 到本地端口
  - 进程 PID 记录到缓存目录，支持清理

幂等规则：
  - support_id UNIQUE：API 端已有 draft 记录 → 返回已存在提示
  - 已有非 draft 状态（published/archived/rejected）→ API 返回已存在信息

调用方：
  - pipeline.py Stage 2（import）
  - CLI: python -m kbd.run import --ids xxx
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .observability import traceparent
from .tunnel import (
    _KB_LOCK_FILE as _PORT_FORWARD_LOCK_FILE,
)
from .tunnel import (
    _KB_LOG_FILE as _PORT_FORWARD_LOG_FILE,
)
from .tunnel import (
    _KB_PID_FILE as _PORT_FORWARD_PID_FILE,
)
from .tunnel import (
    _KB_PROCESS as _PORT_FORWARD_PROCESS,
)
from .tunnel import (
    PortForwardError,
    _check_kb_service_reachable,
    _kubectl_output,
    _namespace_has_service,
    _port_forward_command,
    _port_forward_lock,
    _resolve_k8s_namespace,
    _start_tunnel,
    ensure_kb_service_reachable,
)
from .tunnel import (
    _local_kb_port as _local_port,
)
from .tunnel import (
    _process_identity as _base_process_identity,
)
from .tunnel import (
    _read_pid_metadata as _base_read_pid_metadata,
)
from .tunnel import (
    _stop_kb_port_forward as _stop_port_forward,
)

logger = logging.getLogger("kbd.importer")


def _read_pid_metadata() -> dict[str, Any] | None:
    return _base_read_pid_metadata(_PORT_FORWARD_PID_FILE)


def _process_identity(pid: int) -> dict[str, str]:
    return _base_process_identity(pid)


def _process_matches_metadata(metadata: dict[str, Any]) -> bool:
    try:
        pid = int(metadata["pid"])
        namespace = str(metadata["namespace"])
        local_port = int(metadata["local_port"])
        recorded_identity = metadata["process_identity"]
        recorded_command = metadata["command"]
        current_identity = _process_identity(pid)
    except (IndexError, KeyError, TypeError, ValueError, OSError):
        return False
    expected = _port_forward_command(namespace, local_port)
    return (
        recorded_command == expected
        and recorded_identity == current_identity
        and Path(current_identity["executable"]).name in {"kubectl", "k3s"}
    )


def _terminate_owned_process(metadata: dict[str, Any]) -> None:
    if not _process_matches_metadata(metadata):
        logger.warning("忽略不属于本工具的 PID 文件，避免误杀进程: %s", metadata)
        return
    try:
        pid = int(metadata["pid"])
        os.killpg(pid, signal.SIGTERM)
        logger.info("已终止本工具创建的 port-forward 进程 PID=%d", pid)
    except (ProcessLookupError, OSError, ValueError, KeyError):
        pass


def _validate_port_forward_target(namespace: str) -> None:
    _kubectl_output("get", "service", "kb-service", "-n", namespace, "-o", "name")
    endpoint_data = _kubectl_output(
        "get", "endpointslice", "-n", namespace,
        "-l", "kubernetes.io/service-name=kb-service",
        "-o", "json",
    )
    try:
        endpoint_slices = json.loads(endpoint_data).get("items", [])
        addresses = [
            address
            for item in endpoint_slices
            for endpoint in item.get("endpoints", [])
            if endpoint.get("conditions", {}).get("ready") is not False
            for address in endpoint.get("addresses", [])
        ]
    except (AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise PortForwardError(f"无法解析 {namespace} 的 kb-service EndpointSlice") from exc
    if not addresses:
        raise PortForwardError(
            f"命名空间 {namespace} 的 svc/kb-service 没有可用 EndpointSlice 地址"
        )


def _start_port_forward(namespace: str) -> subprocess.Popen | None:
    local_port = _local_port()
    _validate_port_forward_target(namespace)
    return _start_tunnel(
        "svc/kb-service",
        namespace,
        local_port,
        8004,
        _PORT_FORWARD_PID_FILE,
        _PORT_FORWARD_LOG_FILE,
        _check_kb_service_reachable,
    )


__all__ = [
    "PortForwardError",
    "ensure_kb_service_reachable",
    "import_batch",
    "import_entry",
    "_PORT_FORWARD_LOCK_FILE",
    "_PORT_FORWARD_LOG_FILE",
    "_PORT_FORWARD_PID_FILE",
    "_PORT_FORWARD_PROCESS",
    "_check_kb_service_reachable",
    "_kubectl_output",
    "_local_port",
    "_namespace_has_service",
    "_port_forward_command",
    "_port_forward_lock",
    "_process_identity",
    "_process_matches_metadata",
    "_read_pid_metadata",
    "_resolve_k8s_namespace",
    "_start_port_forward",
    "_stop_port_forward",
    "_terminate_owned_process",
    "_validate_port_forward_target",
]



# ─── API 客户端 ──────────────────────────────────────────────────────────────


async def _call_kbd_ingest_api(
    support_id: str,
    title: str,
    content_md: str | None,
    metadata: dict[str, Any],
    problem_description: str = "",
    alert_info: str = "",
    steps_text: str = "",
    root_cause: str = "",
    solution: str = "",
    operational_impact: str = "",
    is_temporary: str = "",
    recommendations: str = "",
    signals_json: list[dict] | None = None,
    images_json: list[dict] | None = None,
    images: list[dict] | None = None,
    ai_category_id: str | None = None,
    ai_category_conf: float | None = None,
    ai_category_reason: str | None = None,
    client: httpx.AsyncClient | None = None,
    override: bool = False,
    override_status: list[str] | None = None,
) -> dict[str, Any]:
    """
    调用 kb-service KBD 入库 API。

    Args:
        support_id: 案例 ID（幂等键）
        title: 案例标题
        content_md: 聚合渲染 Markdown（含视觉描述）
        metadata: 补充元数据
        problem_description: 问题描述章节
        alert_info: 告警信息章节
        steps_text: 有效排查步骤（自然语言 Markdown）
        root_cause: 根因章节
        solution: 解决方案章节
        operational_impact: 操作影响范围章节
        is_temporary: 是否是临时解决方案章节
        recommendations: 建议与总结章节
        signals_json: 关键信号集合（默认为空列表，由抽取阶段填充）
        ai_category_id: AI 分类建议 ID（可选）
        ai_category_conf: 分类置信度（可选）
        ai_category_reason: 分类理由（可选）
        client: httpx 异步客户端（可选，不传则创建临时客户端）
        override: 强制覆盖已存在的记录
        override_status: 仅覆盖指定状态的记录。None=默认['draft']；['all']=所有状态

    Returns:
        {"success": true, "kbd_id": 123, "status": "draft", "action": "created", "message": "..."}

    Raises:
        httpx.HTTPStatusError: API 返回非 2xx 状态码
        httpx.TimeoutException: 请求超时
    """
    url = f"{settings.KB_SERVICE_URL}/api/kb/kbd/ingest"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
        # 注入 W3C traceparent：kb-service 的 FastAPIInstrumentor 会自动沿用同一 trace_id，
        # 使两端日志可凭 trace_id 串联（见 observability.py）。
        **traceparent(),
    }
    payload = {
        "support_id": support_id,
        "title": title,
        # 8 大章节字段
        "problem_description": problem_description,
        "alert_info": alert_info,
        "steps_text": steps_text,
        "root_cause": root_cause,
        "solution": solution,
        "operational_impact": operational_impact,
        "is_temporary": is_temporary,
        "recommendations": recommendations,
        "signals_json": signals_json if signals_json is not None else [],
        "images_json": images_json if images_json is not None else [],
        "images": images if images is not None else [],
        # 聚合渲染
        "content_md": content_md,
        "metadata": metadata,
        "ai_category_id": ai_category_id,
        "ai_category_conf": ai_category_conf,
        "ai_category_reason": ai_category_reason,
        "override": override,
        "override_status": override_status,
    }

    # 使用传入的 client 或创建临时客户端
    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
        should_close = True

    try:
        # 带重试的请求
        for attempt in range(settings.API_MAX_RETRIES):
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=settings.API_TIMEOUT,
                )
                response.raise_for_status()
                return response.json()

            except httpx.TimeoutException:
                if attempt == settings.API_MAX_RETRIES - 1:
                    raise
                wait = 1.0 * (2 ** attempt)
                logger.warning(
                    "入库 API 超时 support_id=%s 等待 %.1fs 后重试",
                    support_id, wait
                )
                await asyncio.sleep(wait)

            except httpx.HTTPStatusError as exc:
                # 4xx 客户端错误不重试
                if 400 <= exc.response.status_code < 500:
                    logger.error(
                        "入库 API 客户端错误 status=%d support_id=%s",
                        exc.response.status_code, support_id
                    )
                    raise
                # 5xx 服务端错误重试
                if attempt == settings.API_MAX_RETRIES - 1:
                    raise
                wait = 1.0 * (2 ** attempt)
                logger.warning(
                    "入库 API 服务端错误 status=%d 等待 %.1fs 后重试",
                    exc.response.status_code, wait
                )
                await asyncio.sleep(wait)

        raise RuntimeError("unreachable")

    finally:
        if should_close:
            await client.aclose()


# ─── 入库逻辑 ────────────────────────────────────────────────────────────────


async def import_entry(
    support_id: str,
    client: httpx.AsyncClient,
    *,
    override: bool = False,
    override_status: list[str] | None = None,
) -> str:
    """
    将单个案例的处理结果通过 API 写入 kbd_entry。

    Args:
        support_id:      案例 ID（与 raw.json 目录名一致）
        client:          httpx 异步客户端（共享连接）
        override:        强制覆盖已存在的记录
        override_status: 仅覆盖指定状态的记录。None=默认['draft']；['all']=所有状态

    Returns:
        "created" | "overridden" | "skipped" | "error"
    """
    from .converter import convert_kbd_structured

    # 转换：从文件缓存提取结构化章节字段 + content_md + metadata
    result = convert_kbd_structured(support_id)
    if not result:
        # 转换失败或缺少必填 section（已写 abnormal.json）
        logger.warning("案例 %s 转换结果为空，跳过（详见 abnormal.json）", support_id)
        return "error"

    title: str = result["title"]
    content_md: str | None = result.get("content_md")  # None: 由后端 rebuild_content_md 统一渲染
    metadata: dict[str, Any] = result["metadata"]
    # content_md 不再本地校验：新架构下章节字段含占位符，content_md 由后端统一渲染

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    try:
        api_result = await _call_kbd_ingest_api(
            support_id=support_id,
            title=title,
            content_md=content_md,
            metadata=metadata,
            problem_description=result.get("problem_description", ""),
            alert_info=result.get("alert_info", ""),
            steps_text=result.get("steps_text", ""),
            root_cause=result.get("root_cause", ""),
            solution=result.get("solution", ""),
            operational_impact=result.get("operational_impact", ""),
            is_temporary=result.get("is_temporary", ""),
            recommendations=result.get("recommendations", ""),
            signals_json=result.get("signals_json", []),
            images_json=result.get("images_json", []),
            images=result.get("images", []),
            client=client,
            override=override,
            override_status=override_status,
        )

        success = api_result.get("success", False)
        action = api_result.get("action", "")
        message = api_result.get("message", "")

        if success:
            kbd_id = api_result.get("kbd_id")
            status = api_result.get("status", "draft")

            # 根据 action 判断结果
            if action == "created":
                logger.info("案例 %s 已创建（kbd_id=%d status=%s）", support_id, kbd_id, status)
                return "created"
            elif action == "overridden":
                logger.info("案例 %s 已覆盖（kbd_id=%d status=%s）", support_id, kbd_id, status)
                return "overridden"
            elif action == "skipped":
                logger.info("案例 %s 已跳过（kbd_id=%d status=%s reason=%s）", support_id, kbd_id, status, message)
                return "skipped"
            else:
                # 兜底：根据 message 判断
                logger.info("案例 %s 已入库（kbd_id=%d status=%s action=%s）", support_id, kbd_id, status, action)
                return "created"
        else:
            logger.error("案例 %s 入库失败: %s", support_id, message)
            return "error"

    except httpx.HTTPStatusError as exc:
        logger.error("案例 %s API 调用失败 status=%d", support_id, exc.response.status_code)
        return "error"
    except Exception as exc:
        logger.error("案例 %s 入库异常: %s", support_id, exc)
        return "error"


async def import_batch(
    support_ids: list[str],
    _pool: Any = None,  # 废弃参数，保留兼容性
    *,
    override: bool = False,
    override_status: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """
    批量导入 kbd_entry（通过 API）。

    Args:
        support_ids: 要导入的案例 ID 列表
        _pool: 废弃参数（原 asyncpg 连接池），保留向后兼容
        override: 强制覆盖已存在的记录
        override_status: 仅覆盖指定状态的记录。None=默认['draft']；['all']=所有状态
        client: 可选的 httpx 客户端（不传则创建临时客户端）

    Returns:
        统计字段以及 ``results``（本次调用每个 support_id 的权威结果）。
    """
    stats: dict[str, Any] = {
        "created": 0,
        "overridden": 0,
        "skipped": 0,
        "error": 0,
        "results": {},
    }
    total = len(support_ids)

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    if not support_ids:
        logger.debug("批量导入无需调用：本阶段没有待处理 KBD")
        return stats

    # 自动检测并启动 port-forward（k3s ClusterIP 服务本地访问需要）
    if not ensure_kb_service_reachable():
        logger.error("kb-service 不可达，无法执行入库操作")
        stats["error"] = total
        stats["results"] = {support_id: "error" for support_id in support_ids}
        return stats

    # 使用传入的 client 或创建临时客户端
    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
        should_close = True

    try:
        for idx, support_id in enumerate(support_ids, 1):
            logger.info("[%d/%d] 导入案例 %s", idx, total, support_id)
            status = await import_entry(
                support_id, client, override=override, override_status=override_status
            )
            stats[status] = stats.get(status, 0) + 1
            stats["results"][support_id] = status

    finally:
        if should_close:
            await client.aclose()

    logger.info(
        "批量导入完成 created=%d overridden=%d skipped=%d error=%d",
        stats["created"], stats["overridden"], stats["skipped"], stats["error"],
    )
    return stats


# ─── 旧版兼容接口 ────────────────────────────────────────────────────────────────


async def get_pending_review_cases(
    _pool: Any,
    limit: int = 50,
) -> list[dict]:
    """
    查询待审核案例列表（已废弃，应调用 admin-service API）。

    注意：此函数保留向后兼容，但实际应通过 admin-service API 获取。
    如需使用，请调用 GET /api/admin/kb/pending 接口。
    """
    logger.warning(
        "get_pending_review_cases 已废弃，请改用 admin-service API: "
        "GET /api/admin/kb/pending"
    )
    return []
