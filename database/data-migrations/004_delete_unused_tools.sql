-- ============================================================
-- 数据迁移：删除 6 个低频工具
-- Version : 004
-- Issue   : T-TOOL-NAMING-001
-- 说明    : 删除以下 6 个低频工具（均原位于种子 01_tool_definitions.sql）：
--            acli_plugin_asys / acli_plugin_netdoctor / acli_plugin_vm_start /
--            acli_plugin_vm_suspend / get_cluster_detail / get_vm_list
--          同时清理其 dynamic_resource 快照，避免孤儿 revision。
--          删除前已确认无 SOP/KBD/Skill 依赖引用这 6 个工具（见决策文档风险与缓解）。
-- 幂等    : DELETE 对不存在的行无副作用，可安全重复执行。
-- ============================================================

-- ── 1. 清理 dynamic_resource 快照（先于 tool_definition，避免外键/引用悬空）─────
DELETE FROM dynamic_resource_revision
WHERE resource_type = 'tool'
  AND resource_name IN (
    'acli_plugin_asys', 'acli_plugin_netdoctor', 'acli_plugin_vm_start',
    'acli_plugin_vm_suspend', 'get_cluster_detail', 'get_vm_list'
  );

DELETE FROM dynamic_resource_active
WHERE resource_type = 'tool'
  AND resource_name IN (
    'acli_plugin_asys', 'acli_plugin_netdoctor', 'acli_plugin_vm_start',
    'acli_plugin_vm_suspend', 'get_cluster_detail', 'get_vm_list'
  );

-- ── 2. 删除工具定义主表记录 ──────────────────────────────────────────────────
DELETE FROM tool_definition
WHERE tool_name IN (
    'acli_plugin_asys', 'acli_plugin_netdoctor', 'acli_plugin_vm_start',
    'acli_plugin_vm_suspend', 'get_cluster_detail', 'get_vm_list'
);
