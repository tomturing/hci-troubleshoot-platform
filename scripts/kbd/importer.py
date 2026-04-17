"""
scripts/kbd/importer.py — KBD 条目入库（API 调用版）

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
  - pipeline.py Stage 3（import）
  - CLI: python -m scripts.kbd.run import --ids xxx
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("kbd.importer")

# ─── Port-forward 管理 ────────────────────────────────────────────────────────────

_PORT_FORWARD_PID_FILE = settings.KBD_CACHE_DIR.parent / ".kb-service-portforward.pid"
_PORT_FORWARD_PROCESS: subprocess.Popen | None = None


def _check_kb_service_reachable(timeout: float = 2.0) -> bool:
    """快速检测 kb-service 是否可达。"""
    try:
        with httpx.Client(timeout=timeout) as client:
            # 尝试访问 API docs，快速判断服务是否响应
            resp = client.get(f"{settings.KB_SERVICE_URL}/docs", follow_redirects=True)
            return resp.status_code < 500
    except (httpx.ConnectError, httpx.TimeoutException, OSError):
        return False


def _start_port_forward() -> subprocess.Popen | None:
    """
    启动 kubectl port-forward 将 kb-service 暴露到本地。

    Returns:
        启动的 subprocess.Popen 对象，失败返回 None
    """
    # 解析本地端口
    local_port = settings.KB_SERVICE_URL.split(":")[-1].rstrip("/")
    service_name = "kb-service"
    namespace = "hci-dev"

    cmd = [
        "kubectl",
        "port-forward",
        f"svc/{service_name}",
        "-n",
        namespace,
        f"{local_port}:{local_port}",
        "--address",
        "127.0.0.1",
    ]

    logger.info("启动 port-forward: %s", " ".join(cmd))

    try:
        # 启动后台进程，输出重定向到空
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # 创建新会话，避免随父进程终止
        )

        # 等待端口就绪（最多 5 秒）
        for _ in range(10):
            time.sleep(0.5)
            if _check_kb_service_reachable():
                logger.info("port-forward 已就绪 PID=%d", proc.pid)
                # 记录 PID 到文件，便于后续清理
                _PORT_FORWARD_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
                _PORT_FORWARD_PID_FILE.write_text(str(proc.pid))
                return proc
            if proc.poll() is not None:
                logger.error("port-forward 进程已退出 retcode=%d", proc.returncode)
                return None

        logger.warning("port-forward 启动超时，服务仍未就绪")
        proc.terminate()
        return None

    except FileNotFoundError:
        logger.error("kubectl 未安装或不在 PATH 中")
        return None
    except Exception as exc:
        logger.error("启动 port-forward 失败: %s", exc)
        return None


def _stop_port_forward() -> None:
    """停止 port-forward 进程。"""
    global _PORT_FORWARD_PROCESS

    # 先尝试用 PID 文件清理（可能是上次残留）
    if _PORT_FORWARD_PID_FILE.exists():
        try:
            pid = int(_PORT_FORWARD_PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            logger.info("已终止残留 port-forward 进程 PID=%d", pid)
        except (ValueError, ProcessLookupError, OSError):
            pass
        _PORT_FORWARD_PID_FILE.unlink(missing_ok=True)

    # 清理当前进程
    if _PORT_FORWARD_PROCESS and _PORT_FORWARD_PROCESS.poll() is None:
        _PORT_FORWARD_PROCESS.terminate()
        logger.info("已终止当前 port-forward 进程 PID=%d", _PORT_FORWARD_PROCESS.pid)
        _PORT_FORWARD_PROCESS = None


def ensure_kb_service_reachable() -> bool:
    """
    确保 kb-service 可达，自动启动 port-forward（如果需要）。

    Returns:
        True 表示服务可达，False 表示无法连接
    """
    global _PORT_FORWARD_PROCESS

    # 1. 先检测是否已可达（可能是已有 port-forward 或本地 Docker 环境）
    if _check_kb_service_reachable():
        logger.debug("kb-service 已可达，无需 port-forward")
        return True

    # 2. 检查是否有残留 PID 文件
    if _PORT_FORWARD_PID_FILE.exists():
        try:
            pid = int(_PORT_FORWARD_PID_FILE.read_text().strip())
            # 检查进程是否还在运行
            os.kill(pid, 0)  # 发送信号 0 只检测进程存在
            logger.info("发现已有 port-forward 进程 PID=%d，等待就绪", pid)
            # 等待服务就绪
            for _ in range(5):
                time.sleep(0.5)
                if _check_kb_service_reachable():
                    return True
            # 进程存在但服务不可达，可能已僵死，重新启动
            logger.warning("残留 port-forward 进程僵死，重新启动")
            _stop_port_forward()
        except (ValueError, ProcessLookupError, OSError):
            # 进程不存在，清理 PID 文件
            _PORT_FORWARD_PID_FILE.unlink(missing_ok=True)

    # 3. 启动新的 port-forward
    _PORT_FORWARD_PROCESS = _start_port_forward()
    if _PORT_FORWARD_PROCESS:
        return True

    # 4. 最终检测
    return _check_kb_service_reachable()


# ─── API 客户端 ──────────────────────────────────────────────────────────────


async def _call_kbd_ingest_api(
    support_id: str,
    title: str,
    support_url: str | None,
    content_md: str,
    metadata: dict[str, Any],
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
        support_url: 原始案例 URL
        content_md: 结构化 Markdown 内容
        metadata: 补充元数据
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
    }
    payload = {
        "support_id": support_id,
        "support_url": support_url,
        "title": title,
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
    from .converter import convert_case_with_meta

    # 转换：从文件缓存生成 content_md + metadata
    result = convert_case_with_meta(support_id)
    if not result:
        # 转换失败或缺少必填 section（已写 abnormal.json）
        logger.warning("案例 %s 转换结果为空，跳过（详见 abnormal.json）", support_id)
        return "error"

    title: str = result["title"]
    support_url: str = result["support_url"]
    content_md: str = result["content_md"]
    metadata: dict[str, Any] = result["metadata"]

    if not content_md.strip():
        logger.warning("案例 %s content_md 为空，跳过", support_id)
        return "error"

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    try:
        api_result = await _call_kbd_ingest_api(
            support_id=support_id,
            title=title,
            support_url=support_url,
            content_md=content_md,
            metadata=metadata,
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
) -> dict[str, int]:
    """
    批量导入 kbd_entry（通过 API）。

    Args:
        support_ids: 要导入的案例 ID 列表
        _pool: 废弃参数（原 asyncpg 连接池），保留向后兼容
        override: 强制覆盖已存在的记录
        override_status: 仅覆盖指定状态的记录。None=默认['draft']；['all']=所有状态
        client: 可选的 httpx 客户端（不传则创建临时客户端）

    Returns:
        {"created": N, "overridden": N, "skipped": N, "error": N}
    """
    stats: dict[str, int] = {"created": 0, "overridden": 0, "skipped": 0, "error": 0}
    total = len(support_ids)

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    # 自动检测并启动 port-forward（k3s ClusterIP 服务本地访问需要）
    if not ensure_kb_service_reachable():
        logger.error("kb-service 不可达，无法执行入库操作")
        stats["error"] = total
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