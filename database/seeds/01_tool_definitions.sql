-- ===========================================================================
-- database/seeds/01_tool_definitions.sql — 工具定义初始种子数据
-- ===========================================================================
-- 用途：初始化 tool_definition 表，预置 HCI 平台 AI 可调用的工具定义
-- 执行时机：
--   1. 首次建库后（migration 20260404001 执行完毕）
--   2. 使用 ON CONFLICT DO NOTHING，可重复执行（幂等）
-- 执行方法：
--   psql "$DATABASE_URL" -f database/seeds/01_tool_definitions.sql
-- ===========================================================================

-- ─── VM 管理类工具（category='vm'） ──────────────────────────────────────────

INSERT INTO tool_definition (
    tool_name, display_name, tool_type, category, description,
    usage_template, parameters_schema, examples, risk_level, version
) VALUES

-- 获取虚拟机列表（只读）
(
    'acli_vm_list',
    '获取虚拟机列表',
    'acli',
    'vm',
    '获取 HCI 集群内所有虚拟机列表，支持按名称、状态过滤。用于排障初期了解集群虚拟机概况，确认目标虚拟机是否存在。',
    'acli vm list [--filter <key>=<value>]',
    '{
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "过滤条件，格式 key=value，如 power_state=OFF 或 name=test-vm"
            }
        }
    }'::jsonb,
    '[
        {"cmd": "acli vm list", "desc": "列出全部虚拟机"},
        {"cmd": "acli vm list --filter power_state=OFF", "desc": "列出所有关机的虚拟机"},
        {"cmd": "acli vm list --filter name=prod-app-01", "desc": "按名称查找虚拟机"}
    ]'::jsonb,
    1,   -- 只读
    '1.0'
),

-- 获取虚拟机详情（只读）
(
    'acli_vm_get',
    '获取虚拟机详情',
    'acli',
    'vm',
    '获取指定虚拟机的详细信息，包括配置（CPU/内存/磁盘）、网络、当前状态、所在物理主机。用于确认虚拟机配置是否符合预期。',
    'acli vm.get <vm_name_or_uuid>',
    '{
        "type": "object",
        "required": ["vm_name"],
        "properties": {
            "vm_name": {
                "type": "string",
                "description": "虚拟机名称或 UUID"
            }
        }
    }'::jsonb,
    '[
        {"cmd": "acli vm.get prod-app-01", "desc": "按名称获取虚拟机详情"},
        {"cmd": "acli vm.get 550e8400-e29b-41d4-a716-446655440000", "desc": "按 UUID 获取虚拟机详情"}
    ]'::jsonb,
    1,   -- 只读
    '1.0'
),

-- 启动虚拟机（写操作）
(
    'acli_vm_on',
    '启动虚拟机',
    'acli',
    'vm',
    '开机指定虚拟机。仅在确认虚拟机处于关机状态且用户已确认需要开机时调用。开机操作会影响生产环境，必须经用户确认。',
    'acli vm.on <vm_name_or_uuid>',
    '{
        "type": "object",
        "required": ["vm_name"],
        "properties": {
            "vm_name": {
                "type": "string",
                "description": "虚拟机名称或 UUID"
            }
        }
    }'::jsonb,
    '[
        {"cmd": "acli vm.on prod-app-01", "desc": "开机名称为 prod-app-01 的虚拟机"}
    ]'::jsonb,
    2,   -- 写操作
    '1.0'
),

-- 关机虚拟机（高危）
(
    'acli_vm_off',
    '关闭虚拟机',
    'acli',
    'vm',
    '强制关机指定虚拟机（等同于断电）。高危操作，可能导致数据丢失。仅在明确排障需要时调用，且必须获得用户明确授权。',
    'acli vm.off <vm_name_or_uuid>',
    '{
        "type": "object",
        "required": ["vm_name"],
        "properties": {
            "vm_name": {
                "type": "string",
                "description": "虚拟机名称或 UUID"
            }
        }
    }'::jsonb,
    '[
        {"cmd": "acli vm.off prod-app-01", "desc": "强制关机 prod-app-01"}
    ]'::jsonb,
    3,   -- 高危
    '1.0'
)

ON CONFLICT (tool_name) DO NOTHING;

-- ─── 集群状态类工具（category='cluster'） ────────────────────────────────────

INSERT INTO tool_definition (
    tool_name, display_name, tool_type, category, description,
    usage_template, parameters_schema, examples, risk_level, version
) VALUES

-- 获取节点列表（只读）
(
    'acli_host_list',
    '获取集群节点列表',
    'acli',
    'cluster',
    '获取 HCI 集群所有物理节点的状态信息，包括节点名称、IP、在线状态、角色（管理/计算/存储）。用于确认集群节点是否正常。',
    'acli host list',
    '{
        "type": "object",
        "properties": {}
    }'::jsonb,
    '[
        {"cmd": "acli host list", "desc": "列出集群所有物理节点"}
    ]'::jsonb,
    1,   -- 只读
    '1.0'
),

-- 获取集群健康状态（只读）
(
    'scp_get_cluster_health',
    '获取集群健康状态',
    'scp_api',
    'cluster',
    '调用 SCP API 获取集群整体健康状态，包括告警数量、资源负载、服务状态。用于快速评估集群是否健康，定位资源瓶颈。',
    'GET /api/v1/clusters/{cluster_id}/health',
    '{
        "type": "object",
        "required": ["cluster_id"],
        "properties": {
            "cluster_id": {
                "type": "string",
                "description": "集群 ID（可从 acli host list 获取）"
            }
        }
    }'::jsonb,
    '[
        {"endpoint": "GET /api/v1/clusters/cluster-001/health", "desc": "获取集群 cluster-001 的健康状态"}
    ]'::jsonb,
    1,   -- 只读
    '1.0'
)

ON CONFLICT (tool_name) DO NOTHING;

-- ─── 存储类工具（category='storage'） ────────────────────────────────────────

INSERT INTO tool_definition (
    tool_name, display_name, tool_type, category, description,
    usage_template, parameters_schema, examples, risk_level, version
) VALUES

-- 获取存储池状态（只读）
(
    'scp_get_storage_pools',
    '获取存储池状态',
    'scp_api',
    'storage',
    '获取集群存储池的使用率、IOPS、吞吐量等指标。用于排查存储满容量、IOPS 超限等存储类故障。',
    'GET /api/v1/storage/pools',
    '{
        "type": "object",
        "properties": {
            "cluster_id": {
                "type": "string",
                "description": "集群 ID，不填则返回所有集群的存储池"
            }
        }
    }'::jsonb,
    '[
        {"endpoint": "GET /api/v1/storage/pools", "desc": "获取所有存储池状态"},
        {"endpoint": "GET /api/v1/storage/pools?cluster_id=cluster-001", "desc": "获取指定集群的存储池状态"}
    ]'::jsonb,
    1,   -- 只读
    '1.0'
)

ON CONFLICT (tool_name) DO NOTHING;

-- ─── 网络类工具（category='network'） ────────────────────────────────────────

INSERT INTO tool_definition (
    tool_name, display_name, tool_type, category, description,
    usage_template, parameters_schema, examples, risk_level, version
) VALUES

-- 获取网络配置（只读）
(
    'acli_net_list',
    '获取网络配置列表',
    'acli',
    'network',
    '获取集群网络配置信息（VLAN、虚拟网络、上行链路）。用于排查虚拟机网络不通、VLAN 配置错误等网络类故障。',
    'acli net list',
    '{
        "type": "object",
        "properties": {}
    }'::jsonb,
    '[
        {"cmd": "acli net list", "desc": "列出所有虚拟网络配置"}
    ]'::jsonb,
    1,   -- 只读
    '1.0'
)

ON CONFLICT (tool_name) DO NOTHING;

-- ─── 验证结果 ──────────────────────────────────────────────────────────────────

SELECT
    tool_name,
    display_name,
    category,
    risk_level,
    is_active
FROM tool_definition
ORDER BY category, risk_level, tool_name;
