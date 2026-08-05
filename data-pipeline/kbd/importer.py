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
  - pipeline.py Stage 3（import）
  - CLI: python -m kbd.run import --ids xxx
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import subprocess
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import settings
from .observability import traceparent

logger = logging.getLogger("kbd.importer")

# ─── Port-forward 管理 ────────────────────────────────────────────────────────────

_PORT_FORWARD_PID_FILE = settings.KBD_CACHE_DIR.parent / ".kb-service-portforward.pid"
_PORT_FORWARD_LOCK_FILE = settings.KBD_CACHE_DIR.parent / ".kb-service-portforward.lock"
_PORT_FORWARD_LOG_FILE = settings.KBD_CACHE_DIR.parent / ".kb-service-portforward.log"
_PORT_FORWARD_PROCESS: subprocess.Popen | None = None


class PortForwardError(RuntimeError):
    """port-forward 前置检查或启动失败。"""


def _check_kb_service_reachable(timeout: float = 2.0) -> bool:
    """快速检测目标确实是 kb-service，而不只是本地端口有任意 HTTP 服务。"""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{settings.KB_SERVICE_URL}/health", follow_redirects=True)
            if resp.status_code >= 500:
                return False
            payload = resp.json()
            return isinstance(payload, dict) and payload.get("service") == "kb-service"
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError):
        return False
    except ValueError:
        return False


def _kubectl_output(*args: str) -> str:
    """执行只读 kubectl 命令并返回输出；错误保留原始 stderr 便于定位。"""
    try:
        result = subprocess.run(
            ["kubectl", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise PortForwardError("kubectl 未安装或不在 PATH 中") from exc
    except subprocess.TimeoutExpired as exc:
        raise PortForwardError(f"kubectl 命令超时: {' '.join(args)}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PortForwardError(f"kubectl {' '.join(args)} 失败: {detail[:2000]}")
    return result.stdout.strip()


def _namespace_has_service(namespace: str) -> bool:
    """确认候选命名空间中存在 kb-service。"""
    if not namespace or namespace == "default":
        return False
    try:
        _kubectl_output("get", "service", "kb-service", "-n", namespace, "-o", "name")
        return True
    except PortForwardError:
        return False


def _resolve_k8s_namespace() -> str:
    """解析本次连接的命名空间；不能唯一确定时拒绝猜测环境。"""
    configured = os.getenv("KBD_K8S_NAMESPACE") or settings.K8S_NAMESPACE
    if configured:
        if not _namespace_has_service(configured):
            raise PortForwardError(
                f"显式命名空间 {configured!r} 中不存在 svc/kb-service；"
                "请检查 KBD_K8S_NAMESPACE/K8S_NAMESPACE，禁止回退到其他环境"
            )
        return configured

    current = _kubectl_output(
        "config", "view", "--minify", "-o", "jsonpath={..namespace}"
    )
    if _namespace_has_service(current):
        return current

    try:
        role = _kubectl_output(
            "get", "namespace", "argocd", "-o",
            "jsonpath={.metadata.labels.hci\\.env\\.role}",
        )
    except PortForwardError:
        role = ""
    role_namespace = f"hci-{role}" if role in {"dev", "staging", "prod"} else ""
    if _namespace_has_service(role_namespace):
        return role_namespace

    raw_candidates = _kubectl_output(
        "get", "service", "-A", "-o",
        "jsonpath={range .items[?(@.metadata.name==\"kb-service\")]}"
        "{.metadata.namespace}{\"\\n\"}{end}",
    )
    candidates = sorted({item for item in raw_candidates.splitlines() if item.startswith("hci-")})
    if len(candidates) == 1:
        return candidates[0]
    raise PortForwardError(
        "无法唯一确定 kb-service 命名空间；"
        f"候选={candidates or '无'}。请显式设置 KBD_K8S_NAMESPACE，禁止默认连接 hci-dev"
    )


def _validate_port_forward_target(namespace: str) -> None:
    """在启动隧道前验证 Service 和后端地址，避免把配置错误伪装成网络超时。"""
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


def _local_port() -> int:
    parsed = urlparse(settings.KB_SERVICE_URL)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise PortForwardError(
            f"KB_SERVICE_URL={settings.KB_SERVICE_URL!r} 不指向本机；"
            "不会为远端地址自动创建 port-forward"
        )
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def _port_forward_command(namespace: str, local_port: int) -> list[str]:
    return [
        "kubectl", "port-forward", "svc/kb-service", "-n", namespace,
        f"{local_port}:8004", "--address", "127.0.0.1",
    ]


def _read_pid_metadata() -> dict[str, Any] | None:
    try:
        value = json.loads(_PORT_FORWARD_PID_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _process_identity(pid: int) -> dict[str, str]:
    """读取 Linux 进程不可复用的启动时钟与可执行文件身份。"""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    stat_fields = stat[stat.rfind(")") + 2 :].split()
    return {
        # /proc/<pid>/stat 第 22 字段为 starttime；去掉 pid/comm 后索引为 19。
        "start_ticks": stat_fields[19],
        "executable": os.path.realpath(f"/proc/{pid}/exe"),
    }


def _process_matches_metadata(metadata: dict[str, Any]) -> bool:
    """用启动时钟和 executable 校验进程归属，防止 PID 复用后误杀。"""
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
        # k3s 安装中 kubectl 是指向 k3s 多调用二进制的符号链接。
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


@contextmanager
def _port_forward_lock():
    """串行化隧道检查与创建，防止并发 pipeline 抢占同一本地端口。"""
    _PORT_FORWARD_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _PORT_FORWARD_LOCK_FILE.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _start_port_forward(namespace: str) -> subprocess.Popen | None:
    """
    启动 kubectl port-forward 将 kb-service 暴露到本地。

    Returns:
        启动的 subprocess.Popen 对象，失败返回 None
    """
    local_port = _local_port()
    _validate_port_forward_target(namespace)
    cmd = _port_forward_command(namespace, local_port)

    logger.info("启动 port-forward: %s", " ".join(cmd))

    proc: subprocess.Popen | None = None
    output = None
    started = False
    try:
        _PORT_FORWARD_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        output = _PORT_FORWARD_LOG_FILE.open("w+", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)

        # 等待端口就绪（最多 5 秒）
        for _ in range(10):
            time.sleep(0.5)
            if _check_kb_service_reachable():
                logger.info("port-forward 已就绪 PID=%d", proc.pid)
                # 记录 PID 到文件，便于后续清理
                metadata = {
                    "pid": proc.pid,
                    "namespace": namespace,
                    "local_port": local_port,
                    "command": cmd,
                    "process_identity": _process_identity(proc.pid),
                    "started_at": time.time(),
                }
                _PORT_FORWARD_PID_FILE.write_text(
                    json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
                )
                output.close()
                started = True
                return proc
            if proc.poll() is not None:
                output.flush()
                output.seek(0)
                detail = output.read()[-4000:].strip()
                output.close()
                logger.error(
                    "port-forward 进程已退出 retcode=%d namespace=%s 输出=%s",
                    proc.returncode, namespace, detail or "<无输出>",
                )
                return None

        logger.warning("port-forward 启动超时，服务仍未就绪")
        os.killpg(proc.pid, signal.SIGTERM)
        output.close()
        return None

    except FileNotFoundError:
        logger.error("kubectl 未安装或不在 PATH 中")
        return None
    except Exception as exc:
        logger.error("启动 port-forward 失败: %s", exc)
        return None
    finally:
        # 正常运行的子进程已继承日志 fd；父进程无需长期持有文件对象。
        if output is not None and not output.closed:
            output.close()
        # 异常路径不能留下既无 PID 元数据、又占用端口的孤儿进程。
        if proc is not None and proc.poll() is None and not started:
            with suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGTERM)


def _stop_port_forward() -> None:
    """停止 port-forward 进程。"""
    global _PORT_FORWARD_PROCESS

    metadata = _read_pid_metadata()
    if metadata:
        _terminate_owned_process(metadata)
    _PORT_FORWARD_PID_FILE.unlink(missing_ok=True)

    # 清理当前进程
    if _PORT_FORWARD_PROCESS and _PORT_FORWARD_PROCESS.poll() is None:
        with suppress(ProcessLookupError, OSError):
            os.killpg(_PORT_FORWARD_PROCESS.pid, signal.SIGTERM)
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

    try:
        with _port_forward_lock():
            if _check_kb_service_reachable():
                return True
            namespace = _resolve_k8s_namespace()
            metadata = _read_pid_metadata()
            if metadata:
                if (
                    metadata.get("namespace") == namespace
                    and _process_matches_metadata(metadata)
                ):
                    logger.info("发现已有受管 port-forward PID=%s，等待就绪", metadata.get("pid"))
                    for _ in range(5):
                        time.sleep(0.5)
                        if _check_kb_service_reachable():
                            return True
                _terminate_owned_process(metadata)
                _PORT_FORWARD_PID_FILE.unlink(missing_ok=True)

            _PORT_FORWARD_PROCESS = _start_port_forward(namespace)
            return bool(_PORT_FORWARD_PROCESS and _check_kb_service_reachable())
    except PortForwardError as exc:
        logger.error("kb-service 连接前置检查失败: %s", exc)
        return False


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
