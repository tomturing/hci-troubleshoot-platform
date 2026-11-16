"""服务端 → terminal_bridge 的 WebSocket 下发客户端。

与已回滚的 PR #1508 的本质差别：连接由 **case-service 发起**，admin-ui 不知道
bridge 地址、不持有 SSH 凭据、不构造部署命令。凭据只在本模块内存中组装成
``ssh_connect`` 报文，不落库、不写日志、不出现在任何异常文本或返回值里。

协议与 ``terminal_bridge/main.go`` 的 ``InMessage`` 对齐：
``ssh_connect`` → ``ssh_connected`` → ``ssh_exec_process`` → ``exec_result``。
cluster 模式的 ``originAllowed()`` 对空 Origin 放行，服务端无需伪造浏览器。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

Connector = Callable[..., Awaitable[Any]]


class BridgeDeployError(Exception):
    """下发失败；``code`` 直接落到 ``acli_plugin_deployment.error_code``。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


def load_credentials(
    raw_json: str,
    *,
    credential_ref: str = "",
    env_code: str = "",
    credentials_file: str = "",
) -> dict[str, str]:
    """从 Secret 注入的 JSON（或挂载文件）解析目标环境的 SSH 凭据。

    ``password`` 只能填去掉 ``sangfornetwork`` 后缀的**前缀**：bridge 在
    ``newSSHSession`` 里会自动补后缀，填全量会变成双后缀导致登录失败。
    """
    payload = raw_json
    if not payload and credentials_file:
        path = Path(credentials_file)
        if path.is_file():
            payload = path.read_text(encoding="utf-8")
    if not payload:
        raise BridgeDeployError("credential_missing", "服务端未配置 acli 插件下发凭据")

    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise BridgeDeployError("credential_invalid", "凭据 JSON 解析失败") from exc
    if not isinstance(data, dict):
        raise BridgeDeployError("credential_invalid", "凭据 JSON 顶层必须是对象")

    key = credential_ref or env_code
    entry = data.get(key)
    if not isinstance(entry, dict):
        raise BridgeDeployError("credential_missing", f"环境 {key or '(空)'} 未配置凭据")

    auth_type = str(entry.get("auth_type") or "").strip().lower()
    if auth_type not in ("key", "password"):
        raise BridgeDeployError("credential_invalid", "auth_type 只允许 key 或 password")

    credential: dict[str, str] = {
        "auth_type": auth_type,
        "username": str(entry.get("username") or "root"),
    }
    if auth_type == "key":
        private_key = str(entry.get("private_key") or "")
        if not private_key.strip():
            raise BridgeDeployError("credential_missing", f"环境 {key} 缺少 private_key")
        credential["private_key"] = private_key
        credential["passphrase"] = str(entry.get("passphrase") or "")
    else:
        password = str(entry.get("password") or "")
        if not password.strip():
            raise BridgeDeployError("credential_missing", f"环境 {key} 缺少 password")
        if password.endswith("sangfornetwork"):
            raise BridgeDeployError("credential_invalid", "口令必须去掉 sangfornetwork 后缀，bridge 会自动补全")
        credential["password"] = password
    return credential


def resolve_bridge_url(env_bridge_url: str, default_url: str) -> str:
    """环境级覆盖优先；默认走集群 terminal-bridge Service。"""
    url = str(env_bridge_url or "").strip() or str(default_url or "").strip()
    if not url.startswith(("ws://", "wss://")):
        raise BridgeDeployError("bridge_url_invalid", "bridge 地址必须是 ws:// 或 wss://")
    return url


def build_ssh_connect_message(
    *,
    case_id: str,
    node_ip: str,
    ssh_port: int,
    credential: Mapping[str, str],
    trace_id: str,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "ssh_connect",
        "case_id": case_id,
        "host": node_ip,
        "port": int(ssh_port),
        "username": credential["username"],
        "auth_type": credential["auth_type"],
        "node_ip": node_ip,
        "trace_id": trace_id,
    }
    if credential["auth_type"] == "key":
        message["private_key"] = credential["private_key"]
        if credential.get("passphrase"):
            message["passphrase"] = credential["passphrase"]
    else:
        message["password"] = credential["password"]
    return message


def build_exec_message(
    *,
    case_id: str,
    node_ip: str,
    exec_id: str,
    command: str,
    trace_id: str,
    timeout: int,
) -> dict[str, Any]:
    return {
        "type": "ssh_exec_process",
        "case_id": case_id,
        "node_ip": node_ip,
        "exec_id": exec_id,
        "command": command,
        "container": "host",
        "timeout": int(timeout),
        "trace_id": trace_id,
    }


async def _default_connector(url: str, **kwargs: Any) -> Any:
    import websockets

    return await websockets.connect(url, **kwargs)


def _as_int(value: Any, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class AcliPluginBridgeClient:
    """一次部署一条独立 WS 连接；不复用浏览器中继、不共享 SSH 会话。"""

    def __init__(self, *, connector: Connector | None = None, connect_timeout: float = 15.0):
        self._connector: Connector = connector or _default_connector
        self._connect_timeout = connect_timeout

    async def deploy(
        self,
        *,
        bridge_url: str,
        node_ip: str,
        ssh_port: int,
        credential: Mapping[str, str],
        command: str,
        case_id: str,
        exec_id: str,
        trace_id: str,
        timeout: int,
    ) -> dict[str, Any]:
        """返回 bridge 的 exec_result（已规范化），不返回任何凭据。"""
        try:
            ws = await self._connector(
                bridge_url,
                open_timeout=self._connect_timeout,
                close_timeout=self._connect_timeout,
            )
        except BridgeDeployError:
            raise
        except Exception as exc:  # noqa: BLE001 — 网络层异常统一收敛成结构化错误码
            raise BridgeDeployError("bridge_connect_failed", type(exc).__name__) from exc

        try:
            await self._send(ws, build_ssh_connect_message(
                case_id=case_id,
                node_ip=node_ip,
                ssh_port=ssh_port,
                credential=credential,
                trace_id=trace_id,
            ))
            await self._recv_until(ws, expected="ssh_connected", exec_id="", timeout=self._connect_timeout)
            await self._send(ws, build_exec_message(
                case_id=case_id,
                node_ip=node_ip,
                exec_id=exec_id,
                command=command,
                trace_id=trace_id,
                timeout=timeout,
            ))
            result = await self._recv_until(ws, expected="exec_result", exec_id=exec_id, timeout=timeout + 30)
            return {
                "exec_id": str(result.get("exec_id") or exec_id),
                "stdout": str(result.get("stdout") or ""),
                "stderr": str(result.get("stderr") or ""),
                "exit_code": _as_int(result.get("exit_code", result.get("exitCode")), -1),
                "timed_out": bool(result.get("timed_out", False)),
                "error_type": result.get("error_type"),
                "trace_id": trace_id,
            }
        except TimeoutError as exc:
            raise BridgeDeployError("bridge_timeout", "等待 bridge 回执超时") from exc
        except OSError as exc:
            raise BridgeDeployError("bridge_disconnected", type(exc).__name__) from exc
        finally:
            await self._close(ws, case_id)

    async def _send(self, ws: Any, payload: Mapping[str, Any]) -> None:
        await ws.send(json.dumps(dict(payload), ensure_ascii=False))

    async def _recv_until(self, ws: Any, *, expected: str, exec_id: str, timeout: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 1.0)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            try:
                message = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict):
                continue
            message_type = str(message.get("type") or "")
            if message_type == "ssh_error":
                raise BridgeDeployError("ssh_connect_failed", str(message.get("message") or ""))
            if message_type == expected and (not exec_id or message.get("exec_id") == exec_id):
                return message

    async def _close(self, ws: Any, case_id: str) -> None:
        with suppress(Exception):
            await self._send(ws, {"type": "ssh_disconnect", "case_id": case_id})
        with suppress(Exception):
            await ws.close()
