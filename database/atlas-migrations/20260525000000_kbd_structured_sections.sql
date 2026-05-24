-- Atlas 迁移：kbd_entry 增加结构化章节字段
-- 生成时间：2026-05-25
-- 对应 desired_schema.sql 变更：kbd_entry 新增 8 大章节文本列 + steps_json JSONB 列 + 2 个新索引
--
-- 章节字段说明：
--   - 叙述字段（8 个）：由 data-pipeline 从案例 HTML 自动提取，admin 可单独编辑
--   - steps_json：结构化工具步骤（默认为空列表，需 admin 人工/AI 填充后对 InvestigationAgent 可见）
--   - content_md 保留为聚合渲染字段（含截图视觉描述，pipeline 写入；admin 编辑后服务端重建）
--
-- 幂等性：所有变更均使用 IF NOT EXISTS，可安全重复执行

-- 8 大标准章节字段（叙述类，Markdown 格式存储）
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS problem_description text NOT NULL DEFAULT '';
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS alert_info         text NOT NULL DEFAULT '';
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS steps_text        text NOT NULL DEFAULT '';
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS root_cause        text NOT NULL DEFAULT '';
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS solution          text NOT NULL DEFAULT '';
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS operational_impact text NOT NULL DEFAULT '';
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS is_temporary      text NOT NULL DEFAULT '';
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS recommendations   text NOT NULL DEFAULT '';

-- 结构化工具步骤（供 agent-service InvestigationAgent 执行）
-- 格式：[{"tool_name": "...", "tool_args_template": {...}, "expected_pattern": "..."}]
-- 非空时 KBD 条目对 InvestigationAgent 可见（差异诊断）
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS steps_json jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 新索引：steps_json 结构查询（如查找使用特定 tool_name 的 KBD）
CREATE INDEX IF NOT EXISTS idx_kbd_entry_steps_json ON kbd_entry USING GIN (steps_json);

-- 新索引：agent 可用条目快速过滤（steps_json 非空 + published，InvestigationAgent 专用）
CREATE INDEX IF NOT EXISTS idx_kbd_entry_agent_usable ON kbd_entry (category_id, published_at DESC)
    WHERE status = 'published' AND steps_json != '[]'::jsonb;

-- 字段注释（PostgreSQL COMMENT，供开发者查阅）
COMMENT ON COLUMN kbd_entry.problem_description  IS '问题描述（## 问题描述 章节 Markdown，必填）';
COMMENT ON COLUMN kbd_entry.alert_info           IS '告警信息（## 告警信息 章节 Markdown，可选）';
COMMENT ON COLUMN kbd_entry.steps_text           IS '有效排查步骤（自然语言 Markdown，供人阅读；含排查内容章节）';
COMMENT ON COLUMN kbd_entry.root_cause           IS '根因（## 根因 章节 Markdown，必填）';
COMMENT ON COLUMN kbd_entry.solution             IS '解决方案（## 解决方案 章节 Markdown，必填）';
COMMENT ON COLUMN kbd_entry.operational_impact   IS '操作影响范围（## 操作影响范围 章节 Markdown，可选）';
COMMENT ON COLUMN kbd_entry.is_temporary         IS '是否是临时解决方案（## 是否是临时解决方案 章节 Markdown，可选）';
COMMENT ON COLUMN kbd_entry.recommendations      IS '建议与总结（## 建议与总结 章节 Markdown，可选）';
COMMENT ON COLUMN kbd_entry.steps_json           IS '结构化工具步骤（[{tool_name,tool_args_template,expected_pattern}]，默认[]，admin 填充后对 InvestigationAgent 可见）';
