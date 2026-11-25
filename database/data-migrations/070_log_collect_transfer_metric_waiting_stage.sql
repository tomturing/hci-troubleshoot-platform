-- 070：放开 transfer metric 的 waiting 阶段
--
-- 背景（2026-11-25 Q2026112500592）：
--   bridge #1648 新增了 waiting 阶段上报（等待采集 / 等重排），生产端
--   `transferStageFor()` 与展示契约 `contracts/log_collect_display.json` 都已包含
--   waiting，但接收端 Pydantic `Stage` Literal 与 DB `ck_lctm_stage` 只允许
--   fetching/uploading/relaying，于是每条 waiting 样本都被 422 拒绝（本单 1300 条），
--   平台 `detail.transfer` 快照永远缺失 → 面板只能显示"上传中 0/N 片"，
--   把"还没开工"说成"传不动"，同时遮蔽了真正的会话失联。
--
-- 语义：
--   waiting 是"还没开工"的显式阶段（等待采集完成 / 等待重排），不是新的传输方向。
--   放开约束只是让 bridge 已上报的事实能落库；派生层与前端文案早已有该标签。
--   历史数据不需要回填：此前 waiting 从未成功写入，不存在需要纠正的存量。
--
-- 安全措施：
--   1. 幂等：DROP CONSTRAINT IF EXISTS + ADD CONSTRAINT，重跑影响 0 行；
--   2. 不建备份表、不建临时表（Atlas 声明式 schema 会 DROP 未声明的表）；
--   3. 同步 database/desired_schema.sql 的声明，避免下次 Atlas 反向收紧；
--   4. 只放宽枚举，不触碰任何既有行的 stage 值。
--
-- 唯一调用链标记：log-collect-waiting-stage-20261125

BEGIN;

ALTER TABLE log_collect_transfer_metric
    DROP CONSTRAINT IF EXISTS ck_lctm_stage;

-- 表达式必须与 desired_schema.sql 完全同形（含 ::text[] 强制转换），
-- 否则迁移执行后 Atlas schema diff 仍会认为有未收敛的约束差异。
ALTER TABLE log_collect_transfer_metric
    ADD CONSTRAINT ck_lctm_stage
    CHECK ((stage)::text = ANY ((ARRAY['waiting'::character varying, 'fetching'::character varying, 'uploading'::character varying, 'relaying'::character varying])::text[]));

COMMIT;
