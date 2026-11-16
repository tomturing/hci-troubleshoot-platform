"""服务端 → terminal_bridge 的 WebSocket 下发客户端。

边界（2026-11-16 按用户口径订正）：
- 下发目标不是静态环境表，而是 **custom-ui + terminal_bridge 已登录的 SSH 会话**；
  服务端不持有凭据、不发 ``ssh_connect``、不新建 SSH 连接。
- ``list_sessions`` 枚举当前 bridge 进程内已登录会话（case_id + node_ip）；
  ``deploy`` 直接在该会话上发 ``ssh_exec_process``，等 ``exec_result``。
- admin-ui 只提交规格与目标会话，不填 node_ip、口令、私钥、bridge 地址。
- 服务端 WS 握手仍须带同源 Origin（x/net/websocket 的 same-origin 要求）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    if not urlsplit(url).netloc:
        raise BridgeDeployError("bridge_url_invalid", "bridge 地址缺少主机")
    return url


def bridge_origin(bridge_url: str) -> str:
    """由 ws/wss 地址推导与请求 Host 一致的 Origin。

    cluster bridge 的 same-origin 策略比较 ``Origin.Host`` 与 ``r.Host``，
    因此这里必须保留端口；wss 对应 https，ws 对应 http。
    """
    parts = urlsplit(bridge_url)
    scheme = "https" if parts.scheme == "wss" else "http"
    return f"{scheme}://{parts.netloc}"


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
    """一次部署一条独立 WS 连接；复用 bridge 进程内 custom-ui 已建立的 SSH 会话。"""

    def __init__(self, *, connector: Connector | None = None, connect_timeout: float = 15.0):
        self._connector: Connector = connector or _default_connector
        self._connect_timeout = connect_timeout

    async def _connect(self, bridge_url: str) -> Any:
        try:
            return await self._connector(
                bridge_url,
                origin=bridge_origin(bridge_url),
                open_timeout=self._connect_timeout,
                close_timeout=self._connect_timeout,
            )
        except BridgeDeployError:
            raise
        except Exception as exc:  # noqa: BLE001 — 网络层异常统一收敛成结构化错误码
            raise BridgeDeployError("bridge_connect_failed", type(exc).__name__) from exc

    async def list_sessions(self, *, bridge_url: str, trace_id: str) -> list[dict[str, str]]:
        """返回当前 bridge 上已登录的会话摘要；不返回任何凭据。"""
        ws = await self._connect(bridge_url)
        try:
            await self._send(ws, {"type": "list_sessions", "trace_id": trace_id})
            result = await self._recv_until(ws, expected="list_sessions_result", exec_id="", timeout=self._connect_timeout)
            rows = result.get("sessions") or []
            return [
                {
                    "case_id": str(item.get("case_id") or ""),
                    "node_ip": str(item.get("node_ip") or ""),
                    "host": str(item.get("host") or ""),
                    "key": str(item.get("key") or ""),
                }
                for item in rows
                if isinstance(item, dict)
            ]
        except TimeoutError as exc:
            raise BridgeDeployError("bridge_timeout", "等待会话列表超时") from exc
        except OSError as exc:
            raise BridgeDeployError("bridge_disconnected", type(exc).__name__) from exc
        finally:
            with suppress(Exception):
                await ws.close()

    async def deploy(
        self,
        *,
        bridge_url: str,
        case_id: str,
        node_ip: str,
        command: str = "",
        commands: list[str] | None = None,
        exec_id: str,
        trace_id: str,
        timeout: int,
    ) -> dict[str, Any]:
        """在已登录会话上逐条执行部署命令，返回最后一条（执行脚本）的 exec_result。

        大脚本按分片写成多条命令，每条是一条独立 WS 消息，避免 1MB 单条命令
        在 bridge exec 通道卡死且没有 exec_result。
        """
        if not case_id or not node_ip:
            raise BridgeDeployError("session_missing", "目标会话缺少 case_id 或 node_ip")
        steps = list(commands) if commands else [command]
        if not steps or not all(steps):
            raise BridgeDeployError("empty_command", "部署命令为空")
        ws = await self._connect(bridge_url)
        try:
            result: dict[str, Any] = {}
            for index, step in enumerate(steps):
                step_exec_id = f"{exec_id}-{index}" if len(steps) > 1 else exec_id
                await self._send(ws, build_exec_message(
                    case_id=case_id,
                    node_ip=node_ip,
                    exec_id=step_exec_id,
                    command=step,
                    trace_id=trace_id,
                    timeout=timeout,
                ))
                result = await self._recv_until(
                    ws, expected="exec_result", exec_id=step_exec_id, timeout=timeout + 30,
                )
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
            with suppress(Exception):
                await ws.close()

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
