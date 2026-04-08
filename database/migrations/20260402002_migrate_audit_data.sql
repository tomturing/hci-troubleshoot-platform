-- migrate:up

-- =============================================================================
-- 数据迁移：prompt_audit + tool_audit_log → audit_log
-- 创建日期：2026-04-02
--
-- 背景：
--   根据 docs/architecture/20_数据库重规划分析.md，将分散的审计表合并为统一的 audit_log：
--   - prompt_audit → audit_log (audit_type = 'prompt')
--   - tool_audit_log → audit_log (audit_type = 'tool_call')
--
-- 注意：
--   1. 本脚本仅迁移数据，不删除原表（删除由 20260402003_drop_deprecated_tables.sql 处理）
--   2. 使用 ON CONFLICT DO NOTHING 处理重复数据
--   3. 迁移后建议验证数据完整性
-- =============================================================================

-- ─── 1. 迁移 prompt_audit 数据到 audit_log ───────────────────────────────────
-- 幂等：当 prompt_audit 表不存在时跳过

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'prompt_audit') THEN
        INSERT INTO audit_log (
            id, audit_type, conversation_id, turn_index, tool_name, risk_level,
            policy, authorized_by, payload, error, duration_ms, started_at,
            completed_at, trace_id
        )
        SELECT
            audit_id::VARCHAR(36),
            'prompt',
            conversation_id,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL,
            jsonb_build_object(
                'case_id', case_id,
                'assistant_type', assistant_type,
                'model', model,
                'message_count', message_count,
                'has_sop', has_sop,
                'kb_chunks_count', kb_chunks_count,
                'kb_top_score', kb_top_score,
                'system_prompt_chars', system_prompt_chars,
                'messages', messages,
                'payload_ref', payload_ref,
                'user_rating', user_rating
            ),
            NULL,
            NULL,
            captured_at,
            NULL,
            trace_id
        FROM prompt_audit
        ON CONFLICT (id) DO NOTHING;

        RAISE NOTICE 'prompt_audit 迁移完成，源表记录数：%', (SELECT COUNT(*) FROM prompt_audit);
    ELSE
        RAISE NOTICE 'prompt_audit 表不存在，跳过数据迁移（幂等）';
    END IF;
END
$$;

-- ─── 2. 迁移 tool_audit_log 数据到 audit_log ───────────────────────────────────
-- 幂等：当 tool_audit_log 表不存在时跳过（某些环境可能该表已被清理）

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'tool_audit_log') THEN
        INSERT INTO audit_log (
            id,
            audit_type,
            conversation_id,
            turn_index,
            tool_name,
            risk_level,
            policy,
            authorized_by,
            payload,
            error,
            duration_ms,
            started_at,
            completed_at,
            trace_id
        )
        SELECT
            id,
            'tool_call',
            session_id::UUID,
            NULL,
            tool_name,
            risk_level::SMALLINT,
            policy,
            authorized_by,
            jsonb_build_object(
                'tool_args', tool_args,
                'result', result
            ),
            error,
            duration_ms,
            started_at,
            completed_at,
            trace_id
        FROM tool_audit_log
        ON CONFLICT (id) DO NOTHING;

        RAISE NOTICE 'tool_audit_log 迁移完成，源表记录数：%', (SELECT COUNT(*) FROM tool_audit_log);
    ELSE
        RAISE NOTICE 'tool_audit_log 表不存在，跳过数据迁移（幂等）';
    END IF;
END
$$;

-- ─── 3. 迁移 kb_synonym 数据到 kb_category.metadata ───────────────────────────
-- 幂等：当 kb_synonym 表不存在时跳过

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'kb_synonym') THEN
        CREATE TABLE IF NOT EXISTS _kb_synonym_backup AS
        SELECT * FROM kb_synonym WHERE FALSE;  -- 仅复制结构（数据由下方 INSERT 填充）

        INSERT INTO _kb_synonym_backup SELECT * FROM kb_synonym ON CONFLICT DO NOTHING;

        RAISE NOTICE 'kb_synonym 数据已备份到 _kb_synonym_backup 表，记录数：%', (SELECT COUNT(*) FROM kb_synonym);
    ELSE
        RAISE NOTICE 'kb_synonym 表不存在，跳过备份（幂等）';
    END IF;
END
$$;

-- ─── 4. 验证迁移结果 ───────────────────────────────────────────────────────────

DO $$
DECLARE
    total_prompt INTEGER;
    total_tool INTEGER;
    total_audit_log INTEGER;
BEGIN
    SELECT COUNT(*) INTO total_prompt FROM audit_log WHERE audit_type = 'prompt';
    SELECT COUNT(*) INTO total_tool FROM audit_log WHERE audit_type = 'tool_call';
    SELECT COUNT(*) INTO total_audit_log FROM audit_log;

    RAISE NOTICE '=== 迁移验证 ===';
    RAISE NOTICE 'audit_log 总记录数：%', total_audit_log;
    RAISE NOTICE '  - prompt 类型：%', total_prompt;
    RAISE NOTICE '  - tool_call 类型：%', total_tool;
END
$$;

-- migrate:down

-- 回滚：删除已迁移的数据（根据 audit_type 和原始 ID）
DELETE FROM audit_log WHERE audit_type IN ('prompt', 'tool_call');
DROP TABLE IF EXISTS _kb_synonym_backup;

RAISE NOTICE '数据迁移已回滚';