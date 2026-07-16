-- ============================================================
-- 数据迁移：将 11 个点号(.)工具重命名为下划线(snake_case)
-- Version : 003
-- Issue   : T-TOOL-NAMING-001
-- 说明    : 统一工具命名规范（详见 docs/solution/agent/02-架构设计/工具命名规范统一与工具集精简决策.md）。
--          点号工具名（qkv.alert 等）既违反 LLM function name 字符集约束，也与 snake_case 体系不一致。
--          重命名范围：tool_definition / tool_result / dynamic_resource_revision /
--          dynamic_resource_active / dynamic_resource_usage_audit 的 tool_name(resource_name)，
--          以及 system_prompt(kbd_extract_signals_v1) 模板中的 acquirer 示例。
-- 幂等    : 所有 UPDATE 均带 WHERE 旧名条件，已重命名的行不受影响，可安全重复执行。
-- 顺序    : db-migrate.sh 先执行本数据迁移，再执行 atlas schema apply（添加 CHECK 约束），
--          因此存量数据已合规，CHECK 约束应用时不冲突。
-- ============================================================

-- ── 1. tool_definition 主表 ───────────────────────────────────────────────────
UPDATE tool_definition SET tool_name = 'qkv_alert'     WHERE tool_name = 'qkv.alert';
UPDATE tool_definition SET tool_name = 'qkv_task'      WHERE tool_name = 'qkv.task';
UPDATE tool_definition SET tool_name = 'qkv_dialog'    WHERE tool_name = 'qkv.dialog';
UPDATE tool_definition SET tool_name = 'qfk_log'       WHERE tool_name = 'qfk.log';
UPDATE tool_definition SET tool_name = 'qfk_service'   WHERE tool_name = 'qfk.service';
UPDATE tool_definition SET tool_name = 'qfk_system'    WHERE tool_name = 'qfk.system';
UPDATE tool_definition SET tool_name = 'qfk_vm'        WHERE tool_name = 'qfk.vm';
UPDATE tool_definition SET tool_name = 'qfk_network'   WHERE tool_name = 'qfk.network';
UPDATE tool_definition SET tool_name = 'qfk_storage'   WHERE tool_name = 'qfk.storage';
UPDATE tool_definition SET tool_name = 'qfk_hardware'  WHERE tool_name = 'qfk.hardware';
UPDATE tool_definition SET tool_name = 'qfk_platform'  WHERE tool_name = 'qfk.platform';

-- ── 2. tool_result 历史审计（tool_name 外键）─────────────────────────────────
UPDATE tool_result SET tool_name = 'qkv_alert'     WHERE tool_name = 'qkv.alert';
UPDATE tool_result SET tool_name = 'qkv_task'      WHERE tool_name = 'qkv.task';
UPDATE tool_result SET tool_name = 'qkv_dialog'    WHERE tool_name = 'qkv.dialog';
UPDATE tool_result SET tool_name = 'qfk_log'       WHERE tool_name = 'qfk.log';
UPDATE tool_result SET tool_name = 'qfk_service'   WHERE tool_name = 'qfk.service';
UPDATE tool_result SET tool_name = 'qfk_system'    WHERE tool_name = 'qfk.system';
UPDATE tool_result SET tool_name = 'qfk_vm'        WHERE tool_name = 'qfk.vm';
UPDATE tool_result SET tool_name = 'qfk_network'   WHERE tool_name = 'qfk.network';
UPDATE tool_result SET tool_name = 'qfk_storage'   WHERE tool_name = 'qfk.storage';
UPDATE tool_result SET tool_name = 'qfk_hardware'  WHERE tool_name = 'qfk.hardware';
UPDATE tool_result SET tool_name = 'qfk_platform'  WHERE tool_name = 'qfk.platform';

-- ── 3. dynamic_resource_revision（GitOps 快照 resource_name）──────────────────
UPDATE dynamic_resource_revision SET resource_name = 'qkv_alert'     WHERE resource_type = 'tool' AND resource_name = 'qkv.alert';
UPDATE dynamic_resource_revision SET resource_name = 'qkv_task'      WHERE resource_type = 'tool' AND resource_name = 'qkv.task';
UPDATE dynamic_resource_revision SET resource_name = 'qkv_dialog'    WHERE resource_type = 'tool' AND resource_name = 'qkv.dialog';
UPDATE dynamic_resource_revision SET resource_name = 'qfk_log'       WHERE resource_type = 'tool' AND resource_name = 'qfk.log';
UPDATE dynamic_resource_revision SET resource_name = 'qfk_service'   WHERE resource_type = 'tool' AND resource_name = 'qfk.service';
UPDATE dynamic_resource_revision SET resource_name = 'qfk_system'    WHERE resource_type = 'tool' AND resource_name = 'qfk.system';
UPDATE dynamic_resource_revision SET resource_name = 'qfk_vm'        WHERE resource_type = 'tool' AND resource_name = 'qfk.vm';
UPDATE dynamic_resource_revision SET resource_name = 'qfk_network'   WHERE resource_type = 'tool' AND resource_name = 'qfk.network';
UPDATE dynamic_resource_revision SET resource_name = 'qfk_storage'   WHERE resource_type = 'tool' AND resource_name = 'qfk.storage';
UPDATE dynamic_resource_revision SET resource_name = 'qfk_hardware'  WHERE resource_type = 'tool' AND resource_name = 'qfk.hardware';
UPDATE dynamic_resource_revision SET resource_name = 'qfk_platform'  WHERE resource_type = 'tool' AND resource_name = 'qfk.platform';

-- ── 4. dynamic_resource_active（当前激活指针 resource_name）──────────────────
UPDATE dynamic_resource_active SET resource_name = 'qkv_alert'     WHERE resource_type = 'tool' AND resource_name = 'qkv.alert';
UPDATE dynamic_resource_active SET resource_name = 'qkv_task'      WHERE resource_type = 'tool' AND resource_name = 'qkv.task';
UPDATE dynamic_resource_active SET resource_name = 'qkv_dialog'    WHERE resource_type = 'tool' AND resource_name = 'qkv.dialog';
UPDATE dynamic_resource_active SET resource_name = 'qfk_log'       WHERE resource_type = 'tool' AND resource_name = 'qfk.log';
UPDATE dynamic_resource_active SET resource_name = 'qfk_service'   WHERE resource_type = 'tool' AND resource_name = 'qfk.service';
UPDATE dynamic_resource_active SET resource_name = 'qfk_system'    WHERE resource_type = 'tool' AND resource_name = 'qfk.system';
UPDATE dynamic_resource_active SET resource_name = 'qfk_vm'        WHERE resource_type = 'tool' AND resource_name = 'qfk.vm';
UPDATE dynamic_resource_active SET resource_name = 'qfk_network'   WHERE resource_type = 'tool' AND resource_name = 'qfk.network';
UPDATE dynamic_resource_active SET resource_name = 'qfk_storage'   WHERE resource_type = 'tool' AND resource_name = 'qfk.storage';
UPDATE dynamic_resource_active SET resource_name = 'qfk_hardware'  WHERE resource_type = 'tool' AND resource_name = 'qfk.hardware';
UPDATE dynamic_resource_active SET resource_name = 'qfk_platform'  WHERE resource_type = 'tool' AND resource_name = 'qfk.platform';

-- ── 5. dynamic_resource_usage_audit（使用审计 resource_name）─────────────────
UPDATE dynamic_resource_usage_audit SET resource_name = 'qkv_alert'     WHERE resource_type = 'tool' AND resource_name = 'qkv.alert';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qkv_task'      WHERE resource_type = 'tool' AND resource_name = 'qkv.task';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qkv_dialog'    WHERE resource_type = 'tool' AND resource_name = 'qkv.dialog';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qfk_log'       WHERE resource_type = 'tool' AND resource_name = 'qfk.log';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qfk_service'   WHERE resource_type = 'tool' AND resource_name = 'qfk.service';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qfk_system'    WHERE resource_type = 'tool' AND resource_name = 'qfk.system';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qfk_vm'        WHERE resource_type = 'tool' AND resource_name = 'qfk.vm';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qfk_network'   WHERE resource_type = 'tool' AND resource_name = 'qfk.network';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qfk_storage'   WHERE resource_type = 'tool' AND resource_name = 'qfk.storage';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qfk_hardware'  WHERE resource_type = 'tool' AND resource_name = 'qfk.hardware';
UPDATE dynamic_resource_usage_audit SET resource_name = 'qfk_platform'  WHERE resource_type = 'tool' AND resource_name = 'qfk.platform';

-- ── 6. system_prompt(kbd_extract_signals_v1) 模板中的 acquirer 示例 ────────────
-- 采集器目录（acquirer_catalog）由运行时 ACQUIRER_CATALOG 注入，此处仅修正模板内
-- 硬编码的示例 JSON；使用带引号的精确匹配，避免误伤 qfk.log_keyword 等历史命名。
UPDATE system_prompt
SET content_template =
    replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(
        content_template,
        '"qkv.alert"',   '"qkv_alert"'),
        '"qkv.task"',    '"qkv_task"'),
        '"qkv.dialog"',  '"qkv_dialog"'),
        '"qfk.log"',     '"qfk_log"'),
        '"qfk.service"', '"qfk_service"'),
        '"qfk.system"',  '"qfk_system"'),
        '"qfk.vm"',      '"qfk_vm"'),
        '"qfk.network"', '"qfk_network"'),
        '"qfk.storage"', '"qfk_storage"'),
        '"qfk.hardware"', '"qfk_hardware"'),
        '"qfk.platform"', '"qfk_platform"')
WHERE name = 'kbd_extract_signals_v1';
