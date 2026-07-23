-- ===========================================================================
-- 迁移: 20260723000001_delete_kbd_extract_signals_v1_prompt.sql
-- 说明: 彻底下线 v1 关键信号抽取 Prompt (kbd_extract_signals_v1)。
-- 背景: v2 Prompt (kbd_extract_signals_v2) 已上线，LLM 直出 v2 嵌套结构，
--       不再需要 v1 扁平中间态及对应的 Prompt。v1 Prompt 此前作为回滚安全垫保留，
--       现已无需保留——回滚由 git 历史保障（git revert 对应提交即可）。
-- 作用: 从 system_prompt 中删除 v1 行，避免其被误用 / 热加载。
-- 幂等: DELETE ... WHERE 重复执行无副作用。
-- ===========================================================================

DELETE FROM system_prompt
WHERE name = 'kbd_extract_signals_v1';
