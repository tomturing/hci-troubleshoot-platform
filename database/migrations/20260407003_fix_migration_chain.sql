-- migrate:up

-- =============================================================================
-- 迁移链修复 — 解决 20260402002 执行失败导致后续迁移阻塞的问题
-- 创建日期：2026-04-07
--
-- 问题根因：
--   1. schema_migrations 记录了 20260326003 已执行（创建 tool_audit_log）
--   2. 但数据库中 tool_audit_log 表不存在（可能被删除或备份恢复丢失）
--   3. 20260402002_migrate_audit_data.sql 尝试从 tool_audit_log 迁移数据失败
--   4. 后续迁移 20260402003, 20260404001, 20260407002 全部被阻塞
--
-- 修复内容：
--   §1  幂等迁移 prompt_audit 数据（表存在时执行）
--   §2  幂等迁移 tool_audit_log 数据（表存在时执行）
--   §3  删除废弃表（幂等）
--   §4  创建缺失表（session, system_prompt, tool_definition）
--   §5  更新 schema_migrations 记录
--
-- 幂等保证：
--   所有操作检查表/列是否存在后再执行，可安全重复执行
-- =============================================================================

-- ============================================================================
-- §1. 幂等迁移 prompt_audit 数据
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'prompt_audit') THEN
        -- 检查是否有数据需要迁移
        IF EXISTS (SELECT 1 FROM prompt_audit LIMIT 1) THEN
            INSERT INTO audit_log (
                id, audit_type, conversation_id, turn_index, tool_name,
                risk_level, policy, authorized_by, payload, error,
                duration_ms, started_at, completed_at, trace_id
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

            RAISE NOTICE 'prompt_audit 数据迁移完成';
        ELSE
            RAISE NOTICE 'prompt_audit 表为空，跳过迁移';
        END IF;
    ELSE
        RAISE NOTICE 'prompt_audit 表不存在，跳过迁移';
    END IF;
END
$$;

-- ============================================================================
-- §2. 幂等迁移 tool_audit_log 数据
-- ============================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'tool_audit_log') THEN
        IF EXISTS (SELECT 1 FROM tool_audit_log LIMIT 1) THEN
            INSERT INTO audit_log (
                id, audit_type, conversation_id, turn_index, tool_name,
                risk_level, policy, authorized_by, payload, error,
                duration_ms, started_at, completed_at, trace_id
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
                jsonb_build_object('tool_args', tool_args, 'result', result),
                error,
                duration_ms,
                started_at,
                completed_at,
                trace_id
            FROM tool_audit_log
            ON CONFLICT (id) DO NOTHING;

            RAISE NOTICE 'tool_audit_log 数据迁移完成';
        ELSE
            RAISE NOTICE 'tool_audit_log 表为空，跳过迁移';
        END IF;
    ELSE
        RAISE NOTICE 'tool_audit_log 表不存在，跳过迁移（这是预期的，表已在 v6.2 中废弃）';
    END IF;
END
$$;

-- ============================================================================
-- §3. 删除废弃表（幂等）
-- ============================================================================

-- 3.1 删除知识库废弃表
DROP TABLE IF EXISTS kb_chunk CASCADE;
DROP TABLE IF EXISTS kb_document CASCADE;
DROP TABLE IF EXISTS kb_sop_node CASCADE;
DROP TABLE IF EXISTS kb_synonym CASCADE;

-- 3.2 删除数据管道废弃表
DROP TABLE IF EXISTS raw_cases CASCADE;
DROP TABLE IF EXISTS knowledge_atoms CASCADE;

-- 3.3 删除已合并的审计表
DROP TABLE IF EXISTS prompt_audit CASCADE;
DROP TABLE IF EXISTS tool_audit_log CASCADE;

-- 3.4 删除备份表
DROP TABLE IF EXISTS _kb_synonym_backup CASCADE;

-- ============================================================================
-- §4. 创建缺失表（幂等）
-- ============================================================================

-- 4.1 session 表
CREATE TABLE IF NOT EXISTS session (
    session_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    client_id      VARCHAR(255),
    connected_at   TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    disconnected_at TIMESTAMPTZ,
    trace_id       VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_session_conversation ON session(conversation_id);
CREATE INDEX IF NOT EXISTS idx_session_client ON session(client_id);

COMMENT ON TABLE session IS 'WebSocket 会话审计表';

-- 4.2 system_prompt 表
CREATE TABLE IF NOT EXISTS system_prompt (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(100) NOT NULL UNIQUE,
    description   TEXT,
    content       TEXT NOT NULL,
    version       VARCHAR(20) NOT NULL DEFAULT '1.0',
    is_active     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    trace_id      VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_system_prompt_active ON system_prompt(is_active) WHERE is_active = TRUE;

COMMENT ON TABLE system_prompt IS 'Prompt 模板库，版本化管理';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_system_prompt_updated_at') THEN
        CREATE TRIGGER update_system_prompt_updated_at
            BEFORE UPDATE ON system_prompt
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END
$$;

-- 4.3 tool_definition 表
CREATE TABLE IF NOT EXISTS tool_definition (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(100) NOT NULL UNIQUE,
    description   TEXT NOT NULL,
    parameters    JSONB NOT NULL DEFAULT '{}',
    risk_level    SMALLINT NOT NULL DEFAULT 1,
    policy        VARCHAR(20) NOT NULL DEFAULT 'auto',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    trace_id      VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_tool_definition_active ON tool_definition(is_active) WHERE is_active = TRUE;

COMMENT ON TABLE tool_definition IS 'ReAct 工具定义库';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_tool_definition_updated_at') THEN
        CREATE TRIGGER update_tool_definition_updated_at
            BEFORE UPDATE ON tool_definition
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END
$$;

-- ============================================================================
-- §5. 更新 schema_migrations 记录
-- ============================================================================

-- 标记阻塞的迁移为已执行
INSERT INTO schema_migrations (version) VALUES
    ('20260402002'),
    ('20260402003'),
    ('20260404001'),
    ('20260407002')
ON CONFLICT (version) DO NOTHING;

-- ============================================================================
-- §6. 验证最终状态
-- ============================================================================

DO $$
DECLARE
    table_count INTEGER;
    expected_count INTEGER := 17;
    deprecated_tables TEXT[] := ARRAY[
        'kb_document', 'kb_chunk', 'kb_sop_node', 'kb_synonym',
        'raw_cases', 'knowledge_atoms', 'prompt_audit', 'tool_audit_log'
    ];
    required_tables TEXT[] := ARRAY[
        'customer', 'user', 'case', 'environment', 'assistant_evaluation',
        'session', 'conversation', 'message', 'audit_log',
        'diagnostic_item', 'tool_result', 'system_prompt', 'tool_definition',
        'kb_category', 'kbd_entry', 'sop_document', 'sop_chunk'
    ];
    t TEXT;
    found_deprecated TEXT[];
    missing_required TEXT[];
BEGIN
    -- 检查废弃表
    FOREACH t IN ARRAY deprecated_tables LOOP
        IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = t) THEN
            found_deprecated := array_append(found_deprecated, t);
        END IF;
    END LOOP;

    -- 检查必需表
    FOREACH t IN ARRAY required_tables LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = t) THEN
            missing_required := array_append(missing_required, t);
        END IF;
    END LOOP;

    -- 统计表数量
    SELECT COUNT(*) INTO table_count
    FROM pg_tables
    WHERE schemaname = 'public' AND tablename != 'schema_migrations' AND tablename != 'alembic_version';

    RAISE NOTICE '========================================';
    RAISE NOTICE '迁移修复验证';
    RAISE NOTICE '========================================';
    RAISE NOTICE '业务表数量：%/（期望 %）%', table_count, expected_count;

    IF array_length(found_deprecated, 1) > 0 THEN
        RAISE WARNING '废弃表仍存在：%', found_deprecated;
    ELSE
        RAISE NOTICE '所有废弃表已清理';
    END IF;

    IF array_length(missing_required, 1) > 0 THEN
        RAISE WARNING '缺失必需表：%', missing_required;
    ELSE
        RAISE NOTICE '所有必需表已创建';
    END IF;

    RAISE NOTICE '========================================';
END
$$;

-- migrate:down
-- 此迁移不可自动回滚

RAISE NOTICE '迁移链修复不可自动回滚，如需回滚请从备份恢复';