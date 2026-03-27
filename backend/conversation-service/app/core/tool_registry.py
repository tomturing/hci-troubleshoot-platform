"""
工具注册表——所有工具的声明性元数据，风险等级静态声明

风险等级说明：
  1 = 只读操作，自动执行
  2 = 写操作，需用户确认后执行
  3 = 高危操作，直接 block 拒绝执行
"""

from pydantic import BaseModel


class ToolDefinition(BaseModel):
    """工具定义（OpenAI function calling 格式 + 扩展字段）"""

    name: str
    description: str
    parameters: dict          # JSON Schema
    risk_level: int           # 1=只读, 2=写操作需确认, 3=高危禁用
    policy: str               # auto|notify|confirm|block
    category: str             # scp|acli|kb|dialog


# 工具注册表（Phase 3 初始 4 个 SCP 只读工具，Phase 4 可扩展写操作工具）
TOOL_REGISTRY: dict[str, ToolDefinition] = {
    "get_active_alerts": ToolDefinition(
        name="get_active_alerts",
        description=(
            "查询 HCI 平台当前活跃告警列表。用于了解平台当前是否有告警事件，"
            "是意图识别阶段（S0）的必要信息收集步骤。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回告警数量，默认 10，最大 50",
                    "default": 10,
                },
            },
            "required": [],
        },
        risk_level=1,
        policy="auto",
        category="scp",
    ),
    "get_failed_tasks": ToolDefinition(
        name="get_failed_tasks",
        description=(
            "查询 HCI 平台最近的失败操作任务。包含虚拟机开关机失败、存储操作失败等，"
            "是定位故障原因的关键信息来源。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_type": {
                    "type": "string",
                    "description": "任务类型关键词，如'启动虚拟机'、'关闭虚拟机'",
                },
                "begin_time": {
                    "type": "string",
                    "description": "开始时间，格式 YYYY-MM-DD HH:MM:SS，默认 24 小时内",
                },
                "limit": {"type": "integer", "default": 10},
            },
            "required": [],
        },
        risk_level=1,
        policy="auto",
        category="scp",
    ),
    "get_vm_list": ToolDefinition(
        name="get_vm_list",
        description=(
            "查询 HCI 平台上的虚拟机列表，可按名称过滤。"
            "用于确认虚拟机是否存在、当前状态和所在节点。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name_filter": {
                    "type": "string",
                    "description": "虚拟机名称关键词（支持模糊匹配）",
                },
                "limit": {"type": "integer", "default": 20},
            },
            "required": [],
        },
        risk_level=1,
        policy="auto",
        category="scp",
    ),
    "get_cluster_detail": ToolDefinition(
        name="get_cluster_detail",
        description="查询指定集群的详细信息，包括架构类型、许可模式、可用区等。",
        parameters={
            "type": "object",
            "properties": {
                "cluster_id": {
                    "type": "string",
                    "description": "集群 ID",
                }
            },
            "required": ["cluster_id"],
        },
        risk_level=1,
        policy="auto",
        category="scp",
    ),
    # ─── acli Level 1 只读工具 ─────────────────────────────────────────────────
    "acli_system_top": ToolDefinition(
        name="acli_system_top",
        description=(
            "查询 HCI 节点 CPU 和内存实时使用情况（acli system top）。"
            "适用于：VM 启动/关机失败时判断主机是否资源不足。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ip": {"type": "string", "description": "目标节点 IP，留空表示本机"},
            },
            "required": [],
        },
        risk_level=1,
        policy="auto",
        category="acli",
    ),
    "acli_vm_list": ToolDefinition(
        name="acli_vm_list",
        description=(
            "查询 HCI 节点上的虚拟机列表及运行状态（acli vm list）。"
            "用于确认 VM 是否存在以及当前开关机状态。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ip": {"type": "string", "description": "目标节点 IP，留空表示本机"},
            },
            "required": [],
        },
        risk_level=1,
        policy="auto",
        category="acli",
    ),
    "acli_vm_config": ToolDefinition(
        name="acli_vm_config",
        description=(
            "查询指定虚拟机的配置详情（acli vm config get {vm_id}）。"
            "包含 CPU、内存、磁盘、网卡等配置信息，用于排查 VM 配置错误。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "vm_id": {"type": "string", "description": "虚拟机 ID（必填）"},
                "node_ip": {"type": "string", "description": "目标节点 IP"},
            },
            "required": ["vm_id"],
        },
        risk_level=1,
        policy="auto",
        category="acli",
    ),
    "acli_vm_disk_check": ToolDefinition(
        name="acli_vm_disk_check",
        description=(
            "对指定虚拟机执行磁盘健康检查（acli vm disk check {vm_id}）。"
            "用于排查 VM 磁盘损坏、挂载失败等存储相关故障。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "vm_id": {"type": "string", "description": "虚拟机 ID（必填）"},
                "node_ip": {"type": "string", "description": "目标节点 IP"},
            },
            "required": ["vm_id"],
        },
        risk_level=1,
        policy="auto",
        category="acli",
    ),
    "acli_platform_node_list": ToolDefinition(
        name="acli_platform_node_list",
        description=(
            "查询 HCI 集群所有节点列表及健康状态（acli platform node list）。"
            "用于排查节点故障、确认集群节点数量和状态。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ip": {"type": "string", "description": "任一可达节点 IP"},
            },
            "required": [],
        },
        risk_level=1,
        policy="auto",
        category="acli",
    ),
    "acli_storage_disk_list": ToolDefinition(
        name="acli_storage_disk_list",
        description=(
            "查询 HCI 节点存储磁盘列表和状态（acli storage asan disk list）。"
            "用于排查磁盘离线、坏块等存储故障。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ip": {"type": "string", "description": "目标节点 IP"},
            },
            "required": [],
        },
        risk_level=1,
        policy="auto",
        category="acli",
    ),
    "acli_network_nic_list": ToolDefinition(
        name="acli_network_nic_list",
        description=(
            "查询 HCI 节点网卡列表和状态（acli network nic list）。"
            "用于排查网络连通性问题，确认网卡状态和链路聚合配置。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ip": {"type": "string", "description": "目标节点 IP"},
            },
            "required": [],
        },
        risk_level=1,
        policy="auto",
        category="acli",
    ),
    "acli_log_get": ToolDefinition(
        name="acli_log_get",
        description=(
            "获取 HCI 节点最近的平台操作日志（acli log get --lines N）。"
            "建议在查看任务失败原因、服务异常时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ip": {"type": "string", "description": "目标节点 IP"},
                "lines": {"type": "integer", "description": "获取日志行数，默认 100", "default": 100},
            },
            "required": [],
        },
        risk_level=1,
        policy="notify",      # notify：显示"正在获取日志"提示
        category="acli",
    ),
    # ─── acli Level 2 变更工具（需用户确认）────────────────────────────────────
    "acli_service_restart": ToolDefinition(
        name="acli_service_restart",
        description=(
            "重启 HCI 节点上的指定系统服务（acli service {name} restart）。"
            "操作会导致服务短暂中断，必须由用户确认后执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "服务名称，如 exporter、libvirtd"},
                "node_ip": {"type": "string", "description": "目标节点 IP（必填）"},
            },
            "required": ["service_name", "node_ip"],
        },
        risk_level=2,
        policy="confirm",
        category="acli",
    ),
    "acli_network_nic_up": ToolDefinition(
        name="acli_network_nic_up",
        description=(
            "启用 HCI 节点上已禁用的网卡（acli network nic up {nic_name}）。"
            "操作会改变网络流量路径，必须由用户确认后执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "nic_name": {"type": "string", "description": "网卡名称，如 eth0、bond0"},
                "node_ip": {"type": "string", "description": "目标节点 IP（必填）"},
            },
            "required": ["nic_name", "node_ip"],
        },
        risk_level=2,
        policy="confirm",
        category="acli",
    ),
    "acli_netdoctor": ToolDefinition(
        name="acli_netdoctor",
        description=(
            "运行深信服 netdoctor 网络诊断插件，检测节点间网络连通性问题。"
            "会产生一定网络负载，建议在怀疑网络问题时使用，需用户确认。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "node_ip": {"type": "string", "description": "执行诊断的节点 IP（必填）"},
                "target_ip": {"type": "string", "description": "目标检测 IP（可选）"},
            },
            "required": ["node_ip"],
        },
        risk_level=2,
        policy="confirm",
        category="acli",
    ),
}


def get_tools_for_llm() -> list[dict]:
    """返回 OpenAI function calling 格式的工具列表（排除高危工具）"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in TOOL_REGISTRY.values()
        if tool.policy != "block"    # 高危工具（block）不暴露给 LLM
    ]
