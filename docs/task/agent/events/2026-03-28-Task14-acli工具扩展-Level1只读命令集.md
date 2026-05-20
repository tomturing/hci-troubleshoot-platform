---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 14
---

# Task 14：acli 工具扩展——Level 1 只读命令集（P1）

```
你是一名负责 hci-troubleshoot-platform acli 工具扩展的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
目前 ReactExecutor 只集成了 4 个 SCP REST 工具（Task 11）。
acli 是深信服 HCI 节点级诊断命令行，能获取更细粒度的诊断信息：
  - SCP 工具：平台级视角（告警/任务/VM列表）
  - acli 工具：节点级视角（CPU/内存/存储/网络实时状态）

acli 命令分级（见 http://acli.sangfor.com.cn:6888/commandList）：
  Level 1（只读，自动执行）：
    acli system top               → 节点 CPU/内存使用率
    acli system free              → 内存使用详情
    acli system df                → 磁盘空间使用
    acli system ps axuf           → 进程列表
    acli vm list                  → VM 列表及状态
    acli vm config get {vm_id}    → VM 配置详情
    acli vm disk check {vm_id}    → VM 磁盘健康检查
    acli platform info            → 平台版本/许可信息
    acli platform node list       → 节点列表及状态
    acli platform version         → 版本信息
    acli storage asan disk list   → 存储磁盘列表
    acli storage volume list      → 存储卷列表
    acli network nic list         → 网卡列表
    acli network bond list        → 网络 bond 列表
    acli network vxlan list       → VXLAN 信息
    acli log get --lines 100      → 获取最近日志（平台级）

  Level 2（写操作/变更，需用户确认）：
    acli service {name} restart   → 重启服务
    acli service {name} stop      → 停止服务（高危）
    acli network nic up {nic}     → 启用网卡
    acli network nic down {nic}   → 禁用网卡（高危）
    acli vrouter migrate {id}     → VRouter 迁移

  Plugins（需特殊权限，Level 2 策略）：
    acli plugin netdoctor         → 网络诊断工具
    acli plugin vm_start {vm_id}  → 强制启动 VM

acli 的执行方式：通过 SSH 连接到目标节点执行，或调用适配器 API（如果已部署）
本软件系统通过 SSH 连接 HCI 节点执行 acli 命令（节点 IP 从 SCP 节点列表获取）

前置条件：Task 10（ReactExecutor + TOOL_REGISTRY 完成）

【任务目标】
1. 在 TOOL_REGISTRY 中注册 新的 acli 工具（Level 1: 9 个新工具，Level 2: 3 个）
2. 实现 AcliAdapter —— 通过 SSH 在目标节点执行 acli 命令
3. 实现工具组合路由：ReactExecutor 根据 tool_name，路由到 SCPAdapter 或 AcliAdapter
4. 实现 acli 输出的结构化解析（JSON 化）
5. 扩展 TOOL_REGISTRY 中的 Level 2 工具定义（含 risk_level=2）

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/adapters/acli_adapter.py（新建）
  - backend/conversation-service/app/core/tool_registry.py（新增工具注册）
  - backend/conversation-service/app/adapters/tool_router.py（新建，路由到正确适配器）
  - backend/conversation-service/app/services/conversation_service.py（仅修改 ReactExecutor 注入点：将此前注入的 SCPAdapter 替换为 ToolRouter）
只读参考：
  - http://acli.sangfor.com.cn:6888/commandList（内网，需手动确认 acli 输出格式）
  - docs/architecture/各层最优设计.md § Layer 4（acli 命令使用示例）
  - backend/conversation-service/app/adapters/scp_adapter.py（Task 11 产物，参考 execute 接口）

【详细实现步骤】

Step 1：在 TOOL_REGISTRY 中添加 acli 工具

在 tool_registry.py 中追加以下工具（Level 1）：

```python
# Level 1 acli 只读工具（追加到 TOOL_REGISTRY）
"acli_system_top": ToolDefinition(
    name="acli_system_top",
    description="查询 HCI 节点 CPU 和内存实时使用情况（acli system top）。"
                "适用于：VM 启动/关机失败时判断主机是否资源不足。",
    parameters={
        "type": "object",
        "properties": {
            "node_ip": {"type": "string", "description": "节点 IP，留空表示当前节点"},
        },
        "required": []
    },
    risk_level=1, policy="auto", category="acli",
),
"acli_vm_list": ToolDefinition(...),        # acli vm list
"acli_vm_config": ToolDefinition(...),      # acli vm config get {vm_id}
"acli_vm_disk_check": ToolDefinition(...),  # acli vm disk check {vm_id}
"acli_platform_node_list": ToolDefinition(...),  # acli platform node list
"acli_storage_disk_list": ToolDefinition(...),   # acli storage asan disk list
"acli_network_nic_list": ToolDefinition(...),    # acli network nic list
"acli_log_get": ToolDefinition(             # acli log get --lines 100
    name="acli_log_get",
    description="获取 HCI 节点最近的操作日志。建议在查看任务失败原因时使用。",
    parameters={
        "type": "object",
        "properties": {
            "node_ip": {"type": "string"},
            "lines": {"type": "integer", "default": 100},
        },
        "required": []
    },
    risk_level=1, policy="notify", category="acli",  # notify：显示"正在获取日志"提示
),

# Level 2 acli 变更工具（需用户确认）
"acli_service_restart": ToolDefinition(
    name="acli_service_restart",
    description="重启 HCI 节点上的指定服务（acli service {name} restart）。"
                "操作会导致服务短暂中断，需要用户确认。",
    parameters={
        "type": "object",
        "properties": {
            "service_name": {"type": "string", "description": "服务名称，如 exporter、libvirtd"},
            "node_ip": {"type": "string", "description": "目标节点 IP"},
        },
        "required": ["service_name", "node_ip"]
    },
    risk_level=2, policy="confirm", category="acli",
),
"acli_network_nic_up": ToolDefinition(...),  # risk_level=2, policy="confirm"
"acli_netdoctor": ToolDefinition(            # 网络诊断 plugin，Level 2
    name="acli_netdoctor",
    description="运行深信服 netdoctor 网络诊断工具，检测网络连通性问题。"
                "此工具会产生一定网络负载，建议在怀疑网络问题时使用。",
    parameters={
        "type": "object",
        "properties": {
            "node_ip": {"type": "string"},
            "target_ip": {"type": "string", "description": "目标 IP（可选）"},
        },
        "required": ["node_ip"]
    },
    risk_level=2, policy="confirm", category="acli",
),
```

Step 2：实现 AcliAdapter（SSH 执行）

```python
# backend/conversation-service/app/adapters/acli_adapter.py
"""acli 命令适配器：通过 SSH 在 HCI 节点执行 acli 命令"""
import asyncio
import logging
import shlex
from typing import Any
import asyncssh    # 异步 SSH 库（uv add asyncssh）

logger = logging.getLogger(__name__)

class AcliAdapter:
    """
    acli 命令执行适配器，通过 SSH 连接 HCI 节点。
    
    SSH 连接信息从环境变量读取：
      HCI_SSH_USER      → SSH 用户名（默认 admin）
      HCI_SSH_KEY_PATH  → SSH 私钥路径
      HCI_SSH_PASSWORD  → SSH 密码（与密钥二选一）
    """

    TIMEOUT = 30.0

    def __init__(self, username: str, key_path: str | None, password: str | None):
        self.username = username
        self.key_path = key_path
        self.password = password

    @classmethod
    def from_env(cls) -> "AcliAdapter":
        import os
        return cls(
            username=os.environ.get("HCI_SSH_USER", "admin"),
            key_path=os.environ.get("HCI_SSH_KEY_PATH"),
            password=os.environ.get("HCI_SSH_PASSWORD"),
        )

    async def execute(self, tool_name: str, args: dict) -> Any:
        """统一工具执行入口"""
        COMMAND_MAP = {
            "acli_system_top":       lambda a: f"acli system top",
            "acli_vm_list":          lambda a: f"acli vm list",
            "acli_vm_config":        lambda a: f"acli vm config get {a.get('vm_id', '')}",
            "acli_vm_disk_check":    lambda a: f"acli vm disk check {a.get('vm_id', '')}",
            "acli_platform_node_list": lambda a: "acli platform node list",
            "acli_storage_disk_list": lambda a: "acli storage asan disk list",
            "acli_network_nic_list": lambda a: "acli network nic list",
            "acli_log_get":          lambda a: f"acli log get --lines {a.get('lines', 100)}",
            "acli_service_restart":  lambda a: f"acli service {shlex.quote(a['service_name'])} restart",
            "acli_network_nic_up":   lambda a: f"acli network nic up {shlex.quote(a['nic_name'])}",
            "acli_netdoctor":        lambda a: f"acli plugin netdoctor {a.get('target_ip', '')}",
        }
        factory = COMMAND_MAP.get(tool_name)
        if not factory:
            return {"error": f"AcliAdapter 未实现工具: {tool_name}"}

        node_ip = args.get("node_ip", "127.0.0.1")
        command = factory(args)

        return await self._run_ssh(node_ip, command)

    async def _run_ssh(self, host: str, command: str) -> dict:
        """通过 SSH 执行命令，返回结构化结果"""
        try:
            connect_kwargs = {
                "username": self.username,
                "known_hosts": None,    # 生产环境应使用已知主机验证
            }
            if self.key_path:
                connect_kwargs["client_keys"] = [self.key_path]
            elif self.password:
                connect_kwargs["password"] = self.password

            async with asyncssh.connect(host, **connect_kwargs) as conn:
                result = await conn.run(command, timeout=self.TIMEOUT)
                return {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "exit_code": result.exit_status,
                    "command": command,
                    "node": host,
                }
        except asyncio.TimeoutError:
            return {"error": f"SSH 连接 {host} 超时（{self.TIMEOUT}s）", "command": command}
        except asyncssh.PermissionDenied:
            return {"error": f"SSH 认证失败（{host}），请检查密钥/密码配置"}
        except Exception as e:
            logger.error(f"acli SSH 执行失败 [{host}]: {e}")
            return {"error": str(e), "command": command}
```

Step 3：实现 ToolRouter（路由到正确适配器）

```python
# backend/conversation-service/app/adapters/tool_router.py
"""工具路由：根据工具名称路由到正确适配器"""
from .scp_adapter import SCPAdapter
from .acli_adapter import AcliAdapter
from ..core.tool_registry import TOOL_REGISTRY

class ToolRouter:
    def __init__(self, scp: SCPAdapter, acli: AcliAdapter):
        self.adapters = {"scp": scp, "acli": acli}

    async def execute(self, tool_name: str, args: dict):
        tool_def = TOOL_REGISTRY.get(tool_name)
        if not tool_def:
            return {"error": f"未知工具: {tool_name}"}
        adapter = self.adapters.get(tool_def.category)
        if not adapter:
            return {"error": f"工具类别 {tool_def.category} 无对应适配器"}
        return await adapter.execute(tool_name, args)
```

Step 3b（关键）：将 conversation_service.py 中 ReactExecutor 的注入点从 SCPAdapter 替换为 ToolRouter

```python
# 在 conversation_service.py 的服务初始化代码中，找到原 T11 写入的注入方式：
# ❌ T11 遗留的写法（只支持 SCP 工具）
# scp_adapter = SCPAdapter.from_env()
# react_executor = ReactExecutor(tool_executor=scp_adapter, ...)

# ✅ T14 替换为 ToolRouter（同时支持 SCP + acli）
acli_adapter = AcliAdapter.from_env()
scp_adapter = SCPAdapter.from_env()    # 保留原有实例
tool_router = ToolRouter(scp=scp_adapter, acli=acli_adapter)
react_executor = ReactExecutor(
    glm_client=glm_client,
    tool_executor=tool_router,    # 替换注入点
    confirm_service=confirm_service,
    audit_service=audit_service,
    sse_emitter=sse_emitter,
)
```

Step 4：安全检查——命令注入防护

- 所有接受用户输入的参数必须通过 shlex.quote() 转义
- node_ip 参数必须通过正则验证（只允许 IPv4 或域名格式）
- vm_id 参数只允许字母、数字、连字符

```python
import re
_IP_RE = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
_ID_RE = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')

def validate_ip(ip: str) -> str:
    if not _IP_RE.match(ip):
        raise ValueError(f"非法 IP 地址: {ip}")
    return ip

def validate_id(id_: str) -> str:
    if not _ID_RE.match(id_):
        raise ValueError(f"非法 ID 值: {id_}")
    return id_
```

【约束】
- 所有 acli 命令参数必须经过 shlex.quote() 或正则验证（防命令注入）
- SSH 连接超时 30 秒，不重试（诊断场景下需要快速反馈）
- Level 2 工具必须使用 policy="confirm"（不可改为 auto）

【验收标准】
- [ ] acli_system_top 工具调用后，返回 CPU/内存使用率数据
- [ ] acli_service_restart 触发 SSE confirm_request 事件，用户确认后执行
- [ ] 命令注入输入（如 `; rm -rf /`）被 validate_id / shlex.quote 阻止
- [ ] uv run pytest tests/unit/test_acli_adapter.py -v 通过
- [ ] make lint 无新增错误
```

---