-- =============================================================================
-- P4 阶段 V1 补充迁移：conversation 表诊断字段 + prompt_audit 上下文字段
-- =============================================================================
-- 背景：migrate_p4_v1.sql 添加了 raw_cases / knowledge_atoms 表，
--       但 conversation 表的 P4 阶段诊断字段由于疏漏未包含在迁移脚本中，
--       导致代码（ORM 模型）与 DB schema 不一致（见 DUAL-006 事故记录）。
--
-- 说明：所有变更使用 ADD COLUMN IF NOT EXISTS，可重复执行（幂等）。
--
-- 执行方式：
--   psql -U hci_admin -d hci_troubleshoot -f database/migrate_conversation_p4_v1.sql
--   或由部署脚本自动执行（k3s-deploy-dualrepo.sh 步骤 8.5）
-- =============================================================================

\echo '--- migrate_conversation_p4_v1.sql 开始 ---'

-- ──────────────────────────────────────────────────────────────────────────────
-- conversation 表：P4 阶段新增诊断字段
-- ──────────────────────────────────────────────────────────────────────────────
-- diagnostic_stage: 诊断阶段状态机（S0 意图识别 → S1 假设生成 → S2 验证 → S3 确认 → S4 修复 → S5 验证闭环）
ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS diagnostic_stage  VARCHAR(8)   NOT NULL DEFAULT 'S0',
    ADD COLUMN IF NOT EXISTS category_l1       VARCHAR(100),                        -- 故障一级分类（如：虚拟机）
    ADD COLUMN IF NOT EXISTS category_l2       VARCHAR(100),                        -- 故障二级分类（如：开机失败）
    ADD COLUMN IF NOT EXISTS category_id       VARCHAR(32),                         -- 分类 ID（关联知识库）
    ADD COLUMN IF NOT EXISTS hypothesis        JSONB,                               -- 当前诊断假设列表（JSON）
    ADD COLUMN IF NOT EXISTS react_state       JSONB,                               -- ReAct 循环状态（JSON）
    ADD COLUMN IF NOT EXISTS pending_confirm   JSONB;                               -- 待用户确认的操作（JSON）

COMMENT ON COLUMN conversation.diagnostic_stage IS 'P4 诊断阶段: S0=意图识别 S1=假设生成 S2=验证中 S3=待确认 S4=修复执行 S5=验证闭环';
COMMENT ON COLUMN conversation.hypothesis        IS 'P4 当前假设列表，格式: [{"id":"h1","desc":"...","confidence":0.8}]';
COMMENT ON COLUMN conversation.react_state       IS 'P4 ReAct 状态机快照，格式: {"step":"think|act|observe","context":{...}}';
COMMENT ON COLUMN conversation.pending_confirm   IS 'P4 待用户确认操作，格式: {"action":"...","risk":"low|medium|high","cmd":"..."}';

-- ──────────────────────────────────────────────────────────────────────────────
-- prompt_audit 表：AI 提示词上下文拆解字段
-- ──────────────────────────────────────────────────────────────────────────────
-- context_breakdown: 记录提示词各部分来源和 token 数，用于成本分析和优化
ALTER TABLE prompt_audit
    ADD COLUMN IF NOT EXISTS context_breakdown JSONB;

COMMENT ON COLUMN prompt_audit.context_breakdown IS 'AI 提示词各部分 token 拆解，格式: {"system":120,"history":480,"kb_context":200,...}';

\echo '--- migrate_conversation_p4_v1.sql 完成 ---'
