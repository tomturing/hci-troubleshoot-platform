"""
data-pipeline/kbd/tunnel.py — KBD 数据管道依赖服务端口转发与健康探测管理

功能：
  - 自动检测 kb-service（8004）与 PostgreSQL（5432）服务可达性；
  - 若目标配置为本机（127.0.0.1/localhost）且端口未建立，自动通过 kubectl 建立受管的后台端口转发；
  - 基于文件锁（flock）串行化隧道创建，防多并发冲突；
  - 记录 PID 与进程启动时钟元数据（/proc/<pid>/stat），防止 PID 复用误杀；
  - 严格的环境与 Service 校验（fail-closed 契约，禁止静默跨环境回退）。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

import httpx

from .config import settings

logger = logging.getLogger("kbd.tunnel")

_CACHE_PARENT = settings.KBD_CACHE_DIR.parent

_KB_PID_FILE = _CACHE_PARENT / ".kb-service-portforward.pid"
_KB_LOCK_FILE = _CACHE_PARENT / ".kb-service-portforward.lock"
_KB_LOG_FILE = _CACHE_PARENT / ".kb-service-portforward.log"
_KB_PROCESS: subprocess.Popen | None = None

_PG_PID_FILE = _CACHE_PARENT / ".postgres-portforward.pid"
_PG_LOCK_FILE = _CACHE_PARENT / ".postgres-portforward.lock"
_PG_LOG_FILE = _CACHE_PARENT / ".postgres-portforward.log"
_PG_PROCESS: subprocess.Popen | None = None


class PortForwardError(RuntimeError):
    """port-forward 前置检查或启动失败。"""


# ─── kubectl 基础调用 ─────────────────────────────────────────────────────────────


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


def _namespace_has_service(namespace: str, service_name: str = "kb-service") -> bool:
    """确认候选命名空间中存在指定 Service。"""
    if not namespace or namespace == "default":
        return False
    try:
        _kubectl_output("get", "service", service_name, "-n", namespace, "-o", "name")
        return True
    except PortForwardError:
        return False


def _resolve_k8s_namespace(service_name: str = "kb-service") -> str:
    """解析本次连接的命名空间；不能唯一确定时拒绝猜测环境。"""
    configured = os.getenv("KBD_K8S_NAMESPACE") or settings.K8S_NAMESPACE
    if configured:
        if not _namespace_has_service(configured, service_name):
            raise PortForwardError(
                f"显式命名空间 {configured!r} 中不存在 svc/{service_name}；"
                "请检查 KBD_K8S_NAMESPACE/K8S_NAMESPACE，禁止回退到其他环境"
            )
        return configured

    current = _kubectl_output(
        "config", "view", "--minify", "-o", "jsonpath={..namespace}"
    )
    if _namespace_has_service(current, service_name):
        return current

    try:
        role = _kubectl_output(
            "get", "namespace", "argocd", "-o",
            "jsonpath={.metadata.labels.hci\\.env\\.role}",
        )
    except PortForwardError:
        role = ""
    role_namespace = f"hci-{role}" if role in {"dev", "staging", "prod"} else ""
    if _namespace_has_service(role_namespace, service_name):
        return role_namespace

    raw_candidates = _kubectl_output(
        "get", "service", "-A", "-o",
        f"jsonpath={{range .items[?(@.metadata.name==\"{service_name}\")]}}"
        "{.metadata.namespace}{\"\\n\"}{end}",
    )
    candidates = sorted({item for item in raw_candidates.splitlines() if item.startswith("hci-")})
    if len(candidates) == 1:
        return candidates[0]
    raise PortForwardError(
        f"无法唯一确定 {service_name} 命名空间；"
        f"候选={candidates or '无'}。请显式设置 KBD_K8S_NAMESPACE，禁止默认连接 hci-dev"
    )


# ─── 进程元数据与防误杀校验 ─────────────────────────────────────────────────────────


def _process_identity(pid: int) -> dict[str, str]:
    """读取 Linux 进程不可复用的启动时钟与可执行文件身份。"""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    stat_fields = stat[stat.rfind(")") + 2 :].split()
    return {
        "start_ticks": stat_fields[19],
        "executable": os.path.realpath(f"/proc/{pid}/exe"),
    }


def _read_pid_metadata(pid_file: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(pid_file.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _process_matches_metadata(metadata: dict[str, Any], expected_cmd: list[str]) -> bool:
    """用启动时钟和 executable 校验进程归属，防止 PID 复用后误杀。"""
    try:
        pid = int(metadata["pid"])
        recorded_identity = metadata["process_identity"]
        recorded_command = metadata["command"]
        current_identity = _process_identity(pid)
    except (IndexError, KeyError, TypeError, ValueError, OSError):
        return False
    return (
        recorded_command == expected_cmd
        and recorded_identity == current_identity
        and Path(current_identity["executable"]).name in {"kubectl", "k3s"}
    )


def _terminate_owned_process(metadata: dict[str, Any], expected_cmd: list[str]) -> None:
    if not _process_matches_metadata(metadata, expected_cmd):
        logger.warning("忽略不属于本工具的 PID 文件，避免误杀进程: %s", metadata)
        return
    try:
        pid = int(metadata["pid"])
        os.killpg(pid, signal.SIGTERM)
        logger.info("已终止本工具创建的 port-forward 进程 PID=%d", pid)
    except (ProcessLookupError, OSError, ValueError, KeyError):
        pass


@contextmanager
def _port_forward_lock(lock_file: Path):
    """串行化隧道检查与创建，防止并发 pipeline 抢占同一本地端口。"""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ─── 通用隧道管理 ───────────────────────────────────────────────────────────────


def _start_tunnel(
    service_target: str,
    namespace: str,
    local_port: int,
    remote_port: int,
    pid_file: Path,
    log_file: Path,
    check_fn: Callable[[], bool],
) -> subprocess.Popen | None:
    """启动受管的 kubectl port-forward 子进程。"""
    cmd = [
        "kubectl", "port-forward", service_target, "-n", namespace,
        f"{local_port}:{remote_port}", "--address", "127.0.0.1",
    ]
    logger.info("启动 port-forward: %s", " ".join(cmd))

    proc: subprocess.Popen | None = None
    output = None
    started = False
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        output = log_file.open("w+", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)

        for _ in range(10):
            time.sleep(0.5)
            if check_fn():
                logger.info("port-forward 已就绪 PID=%d target=%s", proc.pid, service_target)
                metadata = {
                    "pid": proc.pid,
                    "namespace": namespace,
                    "local_port": local_port,
                    "remote_port": remote_port,
                    "command": cmd,
                    "process_identity": _process_identity(proc.pid),
                    "started_at": time.time(),
                }
                pid_file.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                output.close()
                started = True
                return proc
            if proc.poll() is not None:
                output.flush()
                output.seek(0)
                detail = output.read()[-4000:].strip()
                output.close()
                logger.error(
                    "port-forward 进程已退出 retcode=%d target=%s namespace=%s 输出=%s",
                    proc.returncode, service_target, namespace, detail or "<无输出>",
                )
                return None

        logger.warning("port-forward 启动超时，服务仍未就绪: %s", service_target)
        os.killpg(proc.pid, signal.SIGTERM)
        output.close()
        return None

    except FileNotFoundError:
        logger.error("kubectl 未安装或不在 PATH 中")
        return None
    except Exception as exc:
        logger.error("启动 port-forward 失败 (%s): %s", service_target, exc)
        return None
    finally:
        if output is not None and not output.closed:
            output.close()
        if proc is not None and proc.poll() is None and not started:
            with suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGTERM)


def _ensure_service_tunnel(
    service_name: str,
    local_port: int,
    remote_port: int,
    pid_file: Path,
    lock_file: Path,
    log_file: Path,
    check_fn: Callable[[], bool],
    resolve_service_target: Callable[[str], str] | None = None,
) -> tuple[bool, subprocess.Popen | None]:
    """通用端口转发就绪确保逻辑。"""
    if check_fn():
        logger.debug("%s 已可达，无需建立 port-forward", service_name)
        return True, None

    try:
        with _port_forward_lock(lock_file):
            if check_fn():
                return True, None

            namespace = _resolve_k8s_namespace(service_name)
            target = resolve_service_target(namespace) if resolve_service_target else f"svc/{service_name}"
            cmd = [
                "kubectl", "port-forward", target, "-n", namespace,
                f"{local_port}:{remote_port}", "--address", "127.0.0.1",
            ]

            metadata = _read_pid_metadata(pid_file)
            if metadata:
                if (
                    metadata.get("namespace") == namespace
                    and metadata.get("local_port") == local_port
                    and _process_matches_metadata(metadata, cmd)
                ):
                    logger.info("发现已有受管 port-forward PID=%s，等待就绪", metadata.get("pid"))
                    for _ in range(5):
                        time.sleep(0.5)
                        if check_fn():
                            return True, None
                _terminate_owned_process(metadata, cmd)
                pid_file.unlink(missing_ok=True)

            proc = _start_tunnel(target, namespace, local_port, remote_port, pid_file, log_file, check_fn)
            return bool(proc and check_fn()), proc
    except PortForwardError as exc:
        logger.error("%s 连接前置检查失败: %s", service_name, exc)
        return False, None


# ─── kb-service 专用探测与守护 ──────────────────────────────────────────────────


def _check_kb_service_reachable(timeout: float = 2.0) -> bool:
    """快速检测目标确实是 kb-service，而不只是本地端口有任意 HTTP 服务。"""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{settings.KB_SERVICE_URL}/health", follow_redirects=True)
            if resp.status_code >= 500:
                return False
            payload = resp.json()
            return isinstance(payload, dict) and payload.get("service") == "kb-service"
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError, OSError, ValueError):
        return False


def _local_kb_port() -> int:
    parsed = urlparse(settings.KB_SERVICE_URL)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise PortForwardError(
            f"KB_SERVICE_URL={settings.KB_SERVICE_URL!r} 不指向本机；"
            "不会为远端地址自动创建 port-forward"
        )
    return parsed.port or (443 if parsed.scheme == "https" else 80)


def _validate_kb_service_target(namespace: str) -> None:
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


def _port_forward_command(namespace: str, local_port: int) -> list[str]:
    """生成 kb-service 端口转发命令（保持旧接口兼容）。"""
    return [
        "kubectl", "port-forward", "svc/kb-service", "-n", namespace,
        f"{local_port}:8004", "--address", "127.0.0.1",
    ]


def ensure_kb_service_reachable() -> bool:
    """确保 kb-service 可达，自动启动 port-forward（如果需要）。"""
    global _KB_PROCESS
    local_port = _local_kb_port()

    def _resolve_target(namespace: str) -> str:
        _validate_kb_service_target(namespace)
        return "svc/kb-service"

    ok, proc = _ensure_service_tunnel(
        service_name="kb-service",
        local_port=local_port,
        remote_port=8004,
        pid_file=_KB_PID_FILE,
        lock_file=_KB_LOCK_FILE,
        log_file=_KB_LOG_FILE,
        check_fn=_check_kb_service_reachable,
        resolve_service_target=_resolve_target,
    )
    if proc:
        _KB_PROCESS = proc
    return ok


def _stop_kb_port_forward() -> None:
    """停止 kb-service 的 port-forward 进程（保持向后兼容）。"""
    global _KB_PROCESS
    local_port = 8004
    with suppress(Exception):
        local_port = _local_kb_port()
    cmd = _port_forward_command("default", local_port)
    metadata = _read_pid_metadata(_KB_PID_FILE)
    if metadata:
        expected = metadata.get("command", cmd)
        _terminate_owned_process(metadata, expected)
    _KB_PID_FILE.unlink(missing_ok=True)
    if _KB_PROCESS and _KB_PROCESS.poll() is None:
        with suppress(ProcessLookupError, OSError):
            os.killpg(_KB_PROCESS.pid, signal.SIGTERM)
        logger.info("已终止当前 kb-service port-forward 进程 PID=%d", _KB_PROCESS.pid)
        _KB_PROCESS = None


# ─── PostgreSQL 专用探测与守护 ──────────────────────────────────────────────────


def _check_tcp_port_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """检测指定 TCP 端口是否可连接。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_postgres_reachable(port: int, timeout: float = 1.0) -> bool:
    return _check_tcp_port_reachable("127.0.0.1", port, timeout=timeout)


def _local_postgres_port() -> int | None:
    """提取 DATABASE_URL 中的本地端口；非本地地址返回 None。"""
    database_url = settings.asyncpg_database_url
    parsed = urlsplit(database_url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    return parsed.port or 5432


def _resolve_postgres_service_target(namespace: str) -> str:
    """动态探测命名空间内的 postgres 服务名（优先使用 postgres-external，回退到 postgres）。"""
    if _namespace_has_service(namespace, "postgres-external"):
        return "svc/postgres-external"
    if _namespace_has_service(namespace, "postgres"):
        return "svc/postgres"
    raise PortForwardError(
        f"命名空间 {namespace} 中未找到 svc/postgres-external 或 svc/postgres"
    )


def ensure_postgres_reachable() -> bool:
    """确保 PostgreSQL 可达；若配置指向本地且不可达，自动启动 port-forward。"""
    global _PG_PROCESS
    local_port = _local_postgres_port()
    if local_port is None:
        logger.debug("DATABASE_URL 不指向本机，无需自动创建 PostgreSQL port-forward")
        return True

    def _check() -> bool:
        return _check_postgres_reachable(local_port)

    ok, proc = _ensure_service_tunnel(
        service_name="postgres",
        local_port=local_port,
        remote_port=5432,
        pid_file=_PG_PID_FILE,
        lock_file=_PG_LOCK_FILE,
        log_file=_PG_LOG_FILE,
        check_fn=_check,
        resolve_service_target=_resolve_postgres_service_target,
    )
    if proc:
        _PG_PROCESS = proc
    return ok


def ensure_pipeline_dependencies() -> None:
    """在流水线启动前统一检查并拉起必要的基础设施端口转发。"""
    pg_ok = ensure_postgres_reachable()
    if not pg_ok:
        raise PortForwardError(
            f"PostgreSQL 数据库不可达 (本地端口={_local_postgres_port()}) 且无法自动建立端口转发，"
            "请检查 k3s 集群中 PostgreSQL 状态或手动执行 kubectl port-forward"
        )
    kb_ok = ensure_kb_service_reachable()
    if not kb_ok:
        logger.warning(
            "kb-service 暂不可达且无法自动建立端口转发；非 import/vision/classify/extract 阶段可能不受影响"
        )
