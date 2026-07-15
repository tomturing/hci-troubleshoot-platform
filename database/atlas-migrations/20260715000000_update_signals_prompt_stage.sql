-- ===========================================================================
-- 迁移: 20260715000000_update_signals_prompt_stage.sql
-- 说明: 将关键信号分级抽取 Prompt (kbd_extract_signals_v1) 的 stage 由 KBD 调整至 KEY，
--       使其归属到独立的「KEY 关键信号分级抽取」阶段（admin-ui Prompt 管理页面）。
-- 原因: 历史种子迁移 20260714000001 使用 ON CONFLICT (name) DO NOTHING，
--       对已部署库中的既有记录不会覆盖，故此处显式 UPDATE 修正 stage。
-- 幂等: 仅当 name 匹配且当前 stage 仍为 KBD 时才更新，可重复执行。
-- 参考: frontend/admin/src/views/PromptManageView.vue stages[KEY]
-- ===========================================================================

UPDATE system_prompt
SET stage = 'KEY'
WHERE name = 'kbd_extract_signals_v1'
  AND stage = 'KBD';
