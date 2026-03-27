"""
acli 命令适配器——通过 SSH 在 HCI 节点执行 acli 命令

安全设计：
  - 所有 ID 类参数通过正则白名单校验（防命令注入）
  - 字符串参数通过 shlex.quote() 转义
  - SSH known_hosts 建议生产环境配置（默认关闭方便开发）
  - 连接超时 30s，不重试（诊断场景需要快速反馈）

环境变量：
  HCI_SSH_USER      SSH 用户名（默认 admin）
  HCI_SSH_KEY_PATH  SSH 私钥路径
  HCI_SSH_PASSWORD  SSH 密码（与密钥二选一）
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from typing import Any

logger = logging.getLogger(__name__)

# ─── 输入校验正则 ───────────────────────────────────────────────────────────────
# 仅允许 IPv4 格式，防止通过 node_ip 注入 Shell 元字符
_IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
# VM ID / 服务名 / NIC 名：只允许字母、数字、连字符、下划线，最长 64 字符
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _validate_ip(ip: str) -> str:
    """校验 IPv4 地址，非法时抛出 ValueError"""
    if not _IP_RE.match(ip.strip()):
        raise ValueError(f"非法 IP 地址: {ip!r}")
    # 校验每段值 0-255
    parts = ip.strip().split(".")
    if any(int(p) > 255 for p in parts):
        raise ValueError(f"IP 地址各段超出范围: {ip!r}")
    return ip.strip()


def _validate_safe_id(value: str, field_name: str = "参数") -> str:
    """校验 VM ID / 服务名 / NIC 名，非法时抛出 ValueError"""
    if not _SAFE_ID_RE.match(value.strip()):
        raise ValueError(f"非法{field_name}值: {value!r}，只允许字母数字连字符下划线（最长64位）")
    return value.strip()


class AcliAdapter:
    """
    acli 命令执行适配器，通过 SSH 连接 HCI 节点。

    SSH 连接信息从环境变量读取：
      HCI_SSH_USER      → SSH 用户名（默认 admin）
      HCI_SSH_KEY_PATH  → SSH 私钥路径
      HCI_SSH_PASSWORD  → SSH 密码（与密钥二选一）
    """

    # SSH 执行超时（秒），诊断场景需要快速失败
    TIMEOUT = 30.0

    def __init__(
        self,
        username: str,
        key_path: str | None = None,
        password: str | None = None,
    ) -> None:
        self.username = username
        self.key_path = key_path
        self.password = password

    @classmethod
    def from_env(cls) -> AcliAdapter:
        """从环境变量创建实例"""
        return cls(
            username=os.environ.get("HCI_SSH_USER", "admin"),
            key_path=os.environ.get("HCI_SSH_KEY_PATH"),
            password=os.environ.get("HCI_SSH_PASSWORD"),
        )

    async def execute(self, tool_name: str, args: dict) -> Any:
        """统一工具执行入口，供 ToolRouter 调用"""
        try:
            command, node_ip = self._build_command(tool_name, args)
        except (ValueError, KeyError) as e:
            return {"error": f"参数校验失败: {e}"}

        return await self._run_ssh(node_ip, command)

    def _build_command(self, tool_name: str, args: dict) -> tuple[str, str]:
        """
        根据工具名构造 acli 命令字符串，并返回 (command, node_ip)。
        所有用户输入均经过校验/转义。
        """
        node_ip = _validate_ip(args.get("node_ip", "127.0.0.1")) if args.get("node_ip") else "127.0.0.1"

        match tool_name:
            case "acli_system_top":
                return "acli system top", node_ip

            case "acli_vm_list":
                return "acli vm list", node_ip

            case "acli_vm_config":
                vm_id = _validate_safe_id(args["vm_id"], "VM ID")
                return f"acli vm config get {shlex.quote(vm_id)}", node_ip

            case "acli_vm_disk_check":
                vm_id = _validate_safe_id(args["vm_id"], "VM ID")
                return f"acli vm disk check {shlex.quote(vm_id)}", node_ip

            case "acli_platform_node_list":
                return "acli platform node list", node_ip

            case "acli_storage_disk_list":
                return "acli storage asan disk list", node_ip

            case "acli_network_nic_list":
                return "acli network nic list", node_ip

            case "acli_log_get":
                lines = int(args.get("lines", 100))
                # 限制日志行数在合理范围内
                lines = max(1, min(lines, 500))
                return f"acli log get --lines {lines}", node_ip

            case "acli_service_restart":
                svc = _validate_safe_id(args["service_name"], "服务名")
                return f"acli service {shlex.quote(svc)} restart", node_ip

            case "acli_network_nic_up":
                nic = _validate_safe_id(args["nic_name"], "NIC 名")
                return f"acli network nic up {shlex.quote(nic)}", node_ip

            case "acli_netdoctor":
                target_ip = args.get("target_ip", "")
                if target_ip:
                    target_ip = _validate_ip(target_ip)
                    return f"acli plugin netdoctor {target_ip}", node_ip
                return "acli plugin netdoctor", node_ip

            case _:
                raise ValueError(f"AcliAdapter 未实现工具: {tool_name}")

    async def _run_ssh(self, host: str, command: str) -> dict:
        """通过 asyncssh 在目标节点执行命令，返回结构化结果"""
        try:
            import asyncssh  # type: ignore[import-untyped]
        except ImportError:
            logger.error("asyncssh 未安装，请执行 uv add asyncssh")
            return {"error": "asyncssh 未安装，无法执行 SSH 命令", "command": command}

        connect_kwargs: dict[str, Any] = {
            "username": self.username,
            # 生产环境应替换为 known_hosts 文件路径
            "known_hosts": None,
        }
        if self.key_path:
            connect_kwargs["client_keys"] = [self.key_path]
        elif self.password:
            connect_kwargs["password"] = self.password

        try:
            async with asyncssh.connect(host, **connect_kwargs) as conn:  # type: ignore[misc]
                result = await asyncio.wait_for(
                    conn.run(command),
                    timeout=self.TIMEOUT,
                )
                return {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_status,
                    "command": command,
                    "node": host,
                }
        except TimeoutError:
            logger.warning(f"acli SSH 超时 [{host}] 命令: {command}")
            return {"error": f"SSH 连接 {host} 超时（{self.TIMEOUT}s）", "command": command}
        except Exception as exc:
            # asyncssh 的 PermissionDenied / DisconnectError 等均归类为认证/连接错误
            err_type = type(exc).__name__
            logger.error(f"acli SSH 执行失败 [{host}] {err_type}: {exc}")
            return {"error": f"SSH 错误 ({err_type}): {exc}", "command": command, "node": host}
