-- ===========================================================================
-- 迁移: 20260701000000_remove_trace_id_from_skill_definition.sql
-- 说明: 移除 skill_definition 表中的 trace_id 冗余字段
-- 背景: skill_definition 是静态知识定义表（类似代码文件），不是运行时动态数据
--       trace_id 用于追踪请求调用链，适用于 message、fact 等动态表
--       对于静态配置表，trace_id 无实际用途，属于设计惯性导致的过度设计
-- 参考: desired_schema.sql 设计原则 - trace_id 应仅用于运行时动态数据表
-- ===========================================================================

-- 移除 skill_definition 表的 trace_id 字段
ALTER TABLE skill_definition DROP COLUMN IF EXISTS trace_id;

-- 更新字段注释（移除 trace_id 相关说明）
COMMENT ON TABLE skill_definition IS '技能定义表 — 遵循 Agent Skills Open Standard (agentskills.io)，以 Markdown 知识包形式存储领域专业知识和标准操作流程。静态配置表，无 trace_id 字段';