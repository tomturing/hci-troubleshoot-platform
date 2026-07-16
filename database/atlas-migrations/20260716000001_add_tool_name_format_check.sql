-- ============================================================
-- 迁移：tool_definition.tool_name 命名格式 CHECK 约束
-- Version : 20260716000001
-- Issue   : T-TOOL-NAMING-001
-- 说明    : tool_name 须符合 snake_case 正则 ^[a-z][a-z0-9_]{0,63}$，
--          禁止点号(.)与大写字母。数据迁移 003_rename_dot_tools_to_snake_case
--          已先将存量点号工具重命名为下划线，故本约束应用时不与存量数据冲突。
-- 幂等    : 使用 DO 块判断约束是否存在，可安全重复执行。
-- 参考    : docs/solution/agent/02-架构设计/工具命名规范统一与工具集精简决策.md
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_tool_definition_tool_name_format'
          AND conrelid = 'tool_definition'::regclass
    ) THEN
        ALTER TABLE tool_definition
            ADD CONSTRAINT chk_tool_definition_tool_name_format
            CHECK (tool_name ~ '^[a-z][a-z0-9_]{0,63}$');
    END IF;
END
$$;
