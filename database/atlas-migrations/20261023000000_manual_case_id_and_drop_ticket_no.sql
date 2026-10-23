-- ===========================================================================
-- 迁移: 20261023000000_manual_case_id_and_drop_ticket_no.sql
-- 说明: 工单号支持用户指定（格式与自动生成一致），并移除日志任务的冗余工单号列。
-- 背景: 历史上工单号只由 generate_case_id() 生成。日志收集需要"用户给一个工单号：
--       已存在则复用、不存在则按该号建档"，因此 case_id 允许手工指定；
--       日志任务只需要一个工单号，ticket_no 随之删除（历史数据已核查 0 条不一致）。
-- 要点:
--   1. case_id 加格式 CHECK：Q + 8 位日期 + 5~6 位序号（API 层对用户输入限 5 位）；
--   2. log_collect_task 删除 ticket_no 及其白名单约束；
--   3. generate_case_id() 在 desired_extras.sql 中加固：只统计合法格式，避免
--      手工号触发 CAST 异常导致当天自动建单全部 500。
-- 幂等: DROP ... IF EXISTS / ADD 前先 DROP
-- ===========================================================================

ALTER TABLE "case" DROP CONSTRAINT IF EXISTS ck_case_id_format;
ALTER TABLE "case" ADD CONSTRAINT ck_case_id_format CHECK (case_id ~ '^Q[0-9]{8}[0-9]{5,6}$');

ALTER TABLE log_collect_task DROP CONSTRAINT IF EXISTS ck_log_collect_ticket;
ALTER TABLE log_collect_task DROP COLUMN IF EXISTS ticket_no;
