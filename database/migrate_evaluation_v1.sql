-- HCI Troubleshoot Platform - Database Migration Script
-- Version: v1 (评分评价体系)
-- Date: 2026-03-12
-- Purpose: 实现评分评价体系核心 DDL 变更
--   - case 表新增 close_reason 字段
--   - conversation 表新增 repeat_question_count 字段
--   - assistant_evaluation 表增强评分相关字段
--   - 新建 prompt_audit 表

-- ============================================================================
-- 1. case 表新增 close_reason 字段
-- ============================================================================

-- 工单关闭原因：user_command（用户主动关闭）/ timeout（超时）/ abandon（用户放弃）/ admin_close（管理员强制关闭）
-- 用于被动信号轨道采集，是综合质量评分中权重 20% 的关闭意图维度数据来源
ALTER TABLE "case"
    ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20)
        CHECK (close_reason IN ('user_command', 'timeout', 'abandon', 'admin_close'));

COMMENT ON COLUMN "case".close_reason IS '工单关闭原因：user_command/timeout/abandon/admin_close（被动信号轨道核心数据）';

-- ============================================================================
-- 2. conversation 表新增 repeat_question_count 字段
-- ============================================================================

-- 用户重复提问次数统计：conversation-service 在 save_user_message() 时，
-- 取当前消息与前 5 条用户消息做关键词 Jaccard 相似度，若最高相似度 > 0.5 则计数 +1
-- 用于综合质量评分中权重 15% 的用户重复提问维度（负向信号：重复提问 = AI 回答无效）
ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS repeat_question_count INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN conversation.repeat_question_count IS '会话中用户重复提问次数，由 conversation-service 实时统计（Jaccard 相似度 > 0.5 判定为重复）';

-- ============================================================================
-- 3. assistant_evaluation 表增强
-- ============================================================================

-- 3.1 新增 close_reason 字段：冗余存储关闭原因，避免跨表 JOIN
ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20);

COMMENT ON COLUMN assistant_evaluation.close_reason IS '工单关闭原因（冗余存储，避免跨表 JOIN）';

-- 3.2 新增 session_duration_sec 字段：会话时长（秒），用于解决效率维度计算
ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS session_duration_sec INTEGER;

COMMENT ON COLUMN assistant_evaluation.session_duration_sec IS '会话时长（秒），用于解决效率维度计算';

-- 3.3 新增 repeat_question_count 字段：用户重复提问次数，用于重复提问维度计算
ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS repeat_question_count INTEGER;

COMMENT ON COLUMN assistant_evaluation.repeat_question_count IS '用户重复提问次数（100% 覆盖，始终可采集）';

-- 3.4 新增 composite_score 字段：综合质量分 0-100，由 QualityScoreService 计算
ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS composite_score SMALLINT;

COMMENT ON COLUMN assistant_evaluation.composite_score IS '综合质量分 0-100，由 QualityScoreService 计算（双轨制评价：无用户评分时自动降级为三维模型）';

-- 3.5 新增 score_breakdown 字段：各维度详细分解，JSONB 格式
-- 格式示例：{"close_intent": 90, "efficiency": 70, "user_rating": 80, "ai_quality": 65}
ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS score_breakdown JSONB;

COMMENT ON COLUMN assistant_evaluation.score_breakdown IS '各维度详细分解：{"close_intent": 90, "efficiency": 70, "user_rating": 80, "ai_quality": 65}';

-- 3.6 新增 calculated_at 字段：综合质量分计算时间
ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMPTZ;

COMMENT ON COLUMN assistant_evaluation.calculated_at IS '综合质量分计算时间';

-- 3.7 创建索引：按综合分查询差 case（用于质量预警）
CREATE INDEX IF NOT EXISTS idx_eval_composite_score
    ON assistant_evaluation(composite_score)
    WHERE composite_score IS NOT NULL;

-- 3.8 创建索引：按关闭原因分析（用于用户行为分析）
CREATE INDEX IF NOT EXISTS idx_eval_close_reason
    ON assistant_evaluation(close_reason)
    WHERE close_reason IS NOT NULL;

-- ============================================================================
-- 4. 新建 prompt_audit 表
-- ============================================================================

-- AI 层入口 Prompt 审计镜像表
-- 设计目的：
--   1. 元数据字段（has_sop, kb_chunks_count 等）100% 覆盖采集，始终参与综合质量评分
--   2. 完整 payload（messages 字段）按 10-20% 采样存储，用于深度 review 和 fine-tuning
--   3. 关联回路：用户评分后可回填 user_rating 字段
CREATE TABLE IF NOT EXISTS prompt_audit (
    -- 主键
    audit_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- 关联回路（外键）
    conversation_id     UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    case_id             VARCHAR(20),

    -- AI 助手信息
    assistant_type      VARCHAR(50),
    model               VARCHAR(100),

    -- 元数据（小字段，~200 bytes/条，100% 覆盖采集）
    message_count       INTEGER,                   -- 当前会话消息总数
    has_sop             BOOLEAN DEFAULT FALSE,     -- 是否命中 SOP 决策树
    kb_chunks_count     INTEGER DEFAULT 0,         -- RAG 检索命中的 KB chunks 数量
    kb_top_score        FLOAT DEFAULT 0.0,         -- 最高 KB 相关度分数（0-1）
    system_prompt_chars INTEGER,                   -- System Prompt 字符数

    -- 完整 payload（大字段，5000-20000 chars，按策略采样存储）
    messages            JSONB,                     -- 完整 messages[] 数组（采样时使用）
    payload_ref         VARCHAR(200),              -- MinIO 文件路径（方案 C：大 payload 外置存储）

    -- 关联回路（用户评分后回填）
    user_rating         SMALLINT CHECK (user_rating >= 1 AND user_rating <= 5),

    -- 审计字段
    captured_at         TIMESTAMPTZ DEFAULT NOW(), -- 采集时间
    trace_id            VARCHAR(64)                -- W3C traceparent 调用链 ID
);

-- 创建索引：按 case_id 查询（用于单工单分析）
CREATE INDEX IF NOT EXISTS idx_prompt_audit_case ON prompt_audit(case_id);

-- 创建索引：SOP 未命中查询（用于发现 SOP 覆盖盲区）
CREATE INDEX IF NOT EXISTS idx_prompt_audit_has_sop ON prompt_audit(has_sop) WHERE has_sop = FALSE;

-- 创建索引：有用户评分的记录（用于评分率统计）
CREATE INDEX IF NOT EXISTS idx_prompt_audit_rating ON prompt_audit(user_rating) WHERE user_rating IS NOT NULL;

-- 创建索引：按采集时间排序（用于趋势分析）
CREATE INDEX IF NOT EXISTS idx_prompt_audit_captured_at ON prompt_audit(captured_at DESC);

-- 创建索引：按调用链追踪（用于可观测性）
CREATE INDEX IF NOT EXISTS idx_prompt_audit_trace_id ON prompt_audit(trace_id);

COMMENT ON TABLE prompt_audit IS 'AI 层入口 Prompt 审计镜像：元数据 100% 覆盖采集，完整 payload 按 10-20% 采样存储';
COMMENT ON COLUMN prompt_audit.has_sop IS '是否命中 SOP 决策树（100% 覆盖，参与 AI 能力质量维度评分）';
COMMENT ON COLUMN prompt_audit.kb_chunks_count IS 'RAG 检索命中的 KB chunks 数量（100% 覆盖，参与 AI 能力质量维度评分）';
COMMENT ON COLUMN prompt_audit.kb_top_score IS '最高 KB 相关度分数 0-1（100% 覆盖，参与 AI 能力质量维度评分）';
COMMENT ON COLUMN prompt_audit.messages IS '完整 messages[] 数组（采样存储，用于深度 review 和 fine-tuning）';
COMMENT ON COLUMN prompt_audit.payload_ref IS 'MinIO 文件路径（大 payload 外置存储方案，可选）';
COMMENT ON COLUMN prompt_audit.user_rating IS '用户评分后回填（1-5 星，用于关联回路验证）';

-- ============================================================================
-- 5. 验证查询（可选执行）
-- ============================================================================

-- 验证 case 表新增列
-- SELECT column_name, data_type, is_nullable
-- FROM information_schema.columns
-- WHERE table_name = 'case' AND column_name = 'close_reason';

-- 验证 conversation 表新增列
-- SELECT column_name, data_type, column_default
-- FROM information_schema.columns
-- WHERE table_name = 'conversation' AND column_name = 'repeat_question_count';

-- 验证 assistant_evaluation 表新增列
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_name = 'assistant_evaluation'
-- AND column_name IN ('close_reason', 'session_duration_sec', 'repeat_question_count', 'composite_score', 'score_breakdown', 'calculated_at');

-- 验证 prompt_audit 表存在
-- SELECT COUNT(*) FROM information_schema.tables
-- WHERE table_name = 'prompt_audit';

-- ============================================================================
-- 迁移完成
-- ============================================================================

SELECT 'Migration v1 completed successfully! 评分评价体系 DDL 变更已完成' as status;
