-- ============================================================
-- 迁移：移除 tool_definition.tool_type 列
-- Version : 20260528
-- Issue   : T-TOOL-09（Agent 工具体系 v2.0）
-- 背景    : tool_type 与 category 合并，category 完整表达执行后端归属（scp/acli/sop）
-- ============================================================

-- ── UP ───────────────────────────────────────────────────────────────
-- 步骤 1：删除依赖 tool_type 的索引
DROP INDEX IF EXISTS idx_tool_definition_type_active;

-- 步骤 2：删除 tool_type 列
ALTER TABLE tool_definition DROP COLUMN IF EXISTS tool_type;

-- 步骤 3：更新 category 为 NOT NULL（执行路由依据）
ALTER TABLE tool_definition ALTER COLUMN category SET NOT NULL;

-- 步骤 4：更新 parameters_schema 为 NOT NULL DEFAULT '{}'
ALTER TABLE tool_definition ALTER COLUMN parameters_schema SET NOT NULL;
ALTER TABLE tool_definition ALTER COLUMN parameters_schema SET DEFAULT '{}';

-- 步骤 5：创建新的索引（替代原 idx_tool_definition_type_active）
CREATE INDEX IF NOT EXISTS idx_tool_definition_category_active
    ON tool_definition (category, is_active);

-- 步骤 6：更新注释
COMMENT ON COLUMN tool_definition.category IS '工具类别（执行路由依据）：scp（SCP 平台 REST API）/ acli（HCI 节点执行，含 acli_exec/bash_exec/插件诊断）/ sop（SOP 导航工具）';
COMMENT ON COLUMN tool_definition.risk_level IS '风险等级静态默认值：1=只读查询（auto）/ 2=写操作需确认（confirm）/ 3=高危拦截（block）。注意：对 acli_exec/bash_exec 通用工具，运行时 RiskClassifier 根据命令内容动态判定并覆盖此值；对插件诊断/SCP/SOP 工具，此值为固定值（不动态覆盖）';

-- ── DOWN ─────────────────────────────────────────────────────────────
-- 步骤 1：恢复 tool_type 列
ALTER TABLE tool_definition ADD COLUMN tool_type varchar(20);

-- 步骤 2：根据 category 填充 tool_type 默认值（迁移兼容）
UPDATE tool_definition
SET tool_type = CASE
    WHEN category = 'scp' THEN 'scp_api'
    WHEN category = 'acli' THEN 'acli'
    WHEN category = 'sop' THEN 'sop_nav'
    ELSE 'unknown'
END;

-- 步骤 3：设置 tool_type 为 NOT NULL
ALTER TABLE tool_definition ALTER COLUMN tool_type SET NOT NULL;

-- 步骤 4：恢复 category 为可选（NULL）
ALTER TABLE tool_definition ALTER COLUMN category DROP NOT NULL;

-- 步骤 5：恢复 parameters_schema 为可选
ALTER TABLE tool_definition ALTER COLUMN parameters_schema DROP NOT NULL;
ALTER TABLE tool_definition ALTER COLUMN parameters_schema DROP DEFAULT;

-- 步骤 6：删除新索引
DROP INDEX IF EXISTS idx_tool_definition_category_active;

-- 步骤 7：恢复原索引
CREATE INDEX IF NOT EXISTS idx_tool_definition_type_active
    ON tool_definition (tool_type, is_active);

-- 步骤 8：恢复原注释
COMMENT ON COLUMN tool_definition.tool_type IS '工具类型：acli（Sangfor HCI CLI 工具）/ scp_api（SCP REST API 接口）';
COMMENT ON COLUMN tool_definition.category IS '所属故障域（vm / storage / network / cluster / platform）。NULL 表示通用工具（所有故障域均注入）；非 NULL 则只在对应 category_id 的会话中注入，减少 Prompt token';
COMMENT ON COLUMN tool_definition.risk_level IS '风险等级：1=只读查询（不影响生产）/ 2=写操作（修改状态/配置）/ 3=高危（删除/重启/格式化）；影响 tool_result.policy 的默认策略';