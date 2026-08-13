-- ============================================================
-- HCI 数据库扩展对象（functions + triggers）
--
-- ⚠️  本文件由 psql 幂等应用，在每次 ArgoCD deploy 时执行。
--    执行顺序：本文件在 Atlas schema apply 之后运行
--
-- 管理范围：
--   - CREATE OR REPLACE FUNCTION（幂等，可在表创建前运行）
--   - DROP TRIGGER IF EXISTS + CREATE TRIGGER（需表已存在）
--
-- 开发者：修改函数或触发器时，只需更新本文件
-- ============================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_conversation_message_count()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE conversation SET message_count = message_count + 1
            WHERE conversation_id = NEW.conversation_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE conversation SET message_count = GREATEST(message_count - 1, 0)
            WHERE conversation_id = OLD.conversation_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION generate_case_id()
RETURNS VARCHAR(20) AS $$
DECLARE
    v_today VARCHAR(8);
    v_seq   INTEGER;
BEGIN
    v_today := TO_CHAR(CURRENT_DATE, 'YYYYMMDD');
    -- 事务级排他锁（双参数，无 int32 哈希碰撞风险）：不同天并行，同天串行
    PERFORM pg_advisory_xact_lock(hashtext('generate_case_id'), v_today::integer);
    SELECT COALESCE(MAX(CAST(SUBSTRING(case_id FROM 10 FOR 5) AS INTEGER)), 0) + 1
        INTO v_seq FROM "case"
        WHERE case_id LIKE 'Q' || v_today || '%';
    RETURN 'Q' || v_today || LPAD(v_seq::TEXT, 5, '0');
END;
$$ LANGUAGE plpgsql;

-- ═══════════════════════════════════════════════════════════════
-- 清理 Alembic 遗留对象（幂等，每次 deploy 执行，PR #144 后残留）
--
-- 背景：PR #144 清除了 Alembic 迁移体系，但历史迁移创建的触发器和函数
-- 未同步删除，导致以下问题：
--   1. message 表存在双重计数触发器 —— update_message_count_on_insert /
--      update_message_count_on_delete 与当前 update_conversation_message_count
--      并存，每次 INSERT/DELETE 使 message_count +2/-2
--   2. kbd_entry 表存在冗余触发器 trigger_kbd_entry_updated_at，与
--      update_kbd_entry_updated_at 功能重复（无害但需清理）
--   3. 遗留函数 update_conversation_message_count() 已无触发器引用
--
-- 修复方案：先删遗留触发器，再删遗留函数，顺序不可颠倒
-- ═══════════════════════════════════════════════════════════════
DO $$ BEGIN
  -- 清理 message 表 Alembic 遗留触发器（避免 message_count 双倍计数）
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='message') THEN
    DROP TRIGGER IF EXISTS update_message_count_on_insert ON message;
    DROP TRIGGER IF EXISTS update_message_count_on_delete ON message;
  END IF;
  -- 清理 kbd_entry 表 Alembic 遗留触发器（冗余 updated_at 触发器）
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='kbd_entry') THEN
    DROP TRIGGER IF EXISTS trigger_kbd_entry_updated_at ON kbd_entry;
  END IF;
END $$;
-- 清理 Alembic 遗留函数（Alembic 迁移早期版本创建，已由 fn_update_conversation_message_count 替代）
-- 注意：必须在上方 DROP TRIGGER 之后执行，否则残留触发器依赖会报错
DROP FUNCTION IF EXISTS update_conversation_message_count();

-- 触发器：用 DO $$ 块包裹，仅在目标表存在时执行（保护全新 DB 场景）
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='user') THEN
    DROP TRIGGER IF EXISTS update_user_updated_at ON "user";
    CREATE TRIGGER update_user_updated_at
        BEFORE UPDATE ON "user"
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='customer') THEN
    DROP TRIGGER IF EXISTS update_customer_updated_at ON customer;
    CREATE TRIGGER update_customer_updated_at
        BEFORE UPDATE ON customer
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='case') THEN
    DROP TRIGGER IF EXISTS update_case_updated_at ON "case";
    CREATE TRIGGER update_case_updated_at
        BEFORE UPDATE ON "case"
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='diagnosis_session') THEN
    DROP TRIGGER IF EXISTS update_diagnosis_session_updated_at ON diagnosis_session;
    CREATE TRIGGER update_diagnosis_session_updated_at
        BEFORE UPDATE ON diagnosis_session
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='collection_plan') THEN
    DROP TRIGGER IF EXISTS update_collection_plan_updated_at ON collection_plan;
    CREATE TRIGGER update_collection_plan_updated_at
        BEFORE UPDATE ON collection_plan
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='collection_profile_definition') THEN
    DROP TRIGGER IF EXISTS update_collection_profile_definition_updated_at ON collection_profile_definition;
    CREATE TRIGGER update_collection_profile_definition_updated_at
        BEFORE UPDATE ON collection_profile_definition
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='collector_definition') THEN
    DROP TRIGGER IF EXISTS update_collector_definition_updated_at ON collector_definition;
    CREATE TRIGGER update_collector_definition_updated_at
        BEFORE UPDATE ON collector_definition
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='collector_artifact') THEN
    DROP TRIGGER IF EXISTS update_collector_artifact_updated_at ON collector_artifact;
    CREATE TRIGGER update_collector_artifact_updated_at
        BEFORE UPDATE ON collector_artifact
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='diagnosis_upload_session') THEN
    DROP TRIGGER IF EXISTS update_diagnosis_upload_session_updated_at ON diagnosis_upload_session;
    CREATE TRIGGER update_diagnosis_upload_session_updated_at
        BEFORE UPDATE ON diagnosis_upload_session
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='diagnostic_evidence_bundle') THEN
    DROP TRIGGER IF EXISTS update_diagnostic_evidence_bundle_updated_at ON diagnostic_evidence_bundle;
    CREATE TRIGGER update_diagnostic_evidence_bundle_updated_at
        BEFORE UPDATE ON diagnostic_evidence_bundle
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='diagnosis_processing_job') THEN
    DROP TRIGGER IF EXISTS update_diagnosis_processing_job_updated_at ON diagnosis_processing_job;
    CREATE TRIGGER update_diagnosis_processing_job_updated_at
        BEFORE UPDATE ON diagnosis_processing_job
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='offline_signal_collector_mapping') THEN
    DROP TRIGGER IF EXISTS update_offline_signal_mapping_updated_at ON offline_signal_collector_mapping;
    CREATE TRIGGER update_offline_signal_mapping_updated_at
        BEFORE UPDATE ON offline_signal_collector_mapping
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='supplement_plan') THEN
    DROP TRIGGER IF EXISTS update_supplement_plan_updated_at ON supplement_plan;
    CREATE TRIGGER update_supplement_plan_updated_at
        BEFORE UPDATE ON supplement_plan
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='diagnosis_report') THEN
    DROP TRIGGER IF EXISTS update_diagnosis_report_updated_at ON diagnosis_report;
    CREATE TRIGGER update_diagnosis_report_updated_at
        BEFORE UPDATE ON diagnosis_report
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='diagnosis_deletion_job') THEN
    DROP TRIGGER IF EXISTS update_diagnosis_deletion_job_updated_at ON diagnosis_deletion_job;
    CREATE TRIGGER update_diagnosis_deletion_job_updated_at
        BEFORE UPDATE ON diagnosis_deletion_job
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='message') THEN
    DROP TRIGGER IF EXISTS update_conversation_message_count ON message;
    CREATE TRIGGER update_conversation_message_count
        AFTER INSERT OR DELETE ON message
        FOR EACH ROW EXECUTE FUNCTION fn_update_conversation_message_count();
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='diagnostic_item') THEN
    DROP TRIGGER IF EXISTS update_diagnostic_item_updated_at ON diagnostic_item;
    CREATE TRIGGER update_diagnostic_item_updated_at
        BEFORE UPDATE ON diagnostic_item
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='kbd_entry') THEN
    DROP TRIGGER IF EXISTS update_kbd_entry_updated_at ON kbd_entry;
    CREATE TRIGGER update_kbd_entry_updated_at
        BEFORE UPDATE ON kbd_entry
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='kbd_batch_job') THEN
    ALTER TABLE kbd_batch_job
      ADD COLUMN IF NOT EXISTS work_total_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS work_completed_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS work_failed_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS interrupted_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS retry_of_batch_id uuid,
      ADD COLUMN IF NOT EXISTS request_json jsonb NOT NULL DEFAULT '{}'::jsonb;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_kbd_batch_job_retry_of') THEN
      ALTER TABLE kbd_batch_job ADD CONSTRAINT fk_kbd_batch_job_retry_of
        FOREIGN KEY (retry_of_batch_id) REFERENCES kbd_batch_job (batch_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_kbd_batch_job_retry_of') THEN
      ALTER TABLE kbd_batch_job ADD CONSTRAINT uq_kbd_batch_job_retry_of UNIQUE (retry_of_batch_id);
    END IF;
    CREATE INDEX IF NOT EXISTS idx_kbd_batch_job_retry_of
      ON kbd_batch_job (retry_of_batch_id) WHERE retry_of_batch_id IS NOT NULL;
    ALTER TABLE kbd_batch_job DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_status;
    ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_status CHECK (
      (status)::text = ANY (
        (ARRAY[
          'pending'::varchar, 'running'::varchar, 'completed'::varchar,
          'partial_failed'::varchar, 'failed'::varchar, 'interrupted'::varchar
        ])::text[]
      )
    );
    ALTER TABLE kbd_batch_job DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_type;
    ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_type CHECK (
      (job_type)::text = ANY (
        (ARRAY[
          'reanalyze_images'::varchar, 'reclassify'::varchar, 'extract_signals'::varchar,
          'approve'::varchar, 'reject'::varchar
        ])::text[]
      )
    );
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kbd_batch_job_request_json') THEN
      ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_request_json
        CHECK (jsonb_typeof(request_json) = 'object');
    END IF;
    ALTER TABLE kbd_batch_job DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_counts;
    ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_counts CHECK (
      total_count > 0 AND completed_count >= 0 AND succeeded_count >= 0
      AND failed_count >= 0 AND interrupted_count >= 0
      AND completed_count = succeeded_count + failed_count + interrupted_count
      AND completed_count <= total_count
    );
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kbd_batch_job_work_counts') THEN
      ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_work_counts CHECK (
        work_total_count >= 0 AND work_completed_count >= 0 AND work_failed_count >= 0
        AND work_completed_count <= work_total_count AND work_failed_count <= work_completed_count
      );
    END IF;
    DROP TRIGGER IF EXISTS update_kbd_batch_job_updated_at ON kbd_batch_job;
    CREATE TRIGGER update_kbd_batch_job_updated_at
        BEFORE UPDATE ON kbd_batch_job
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='kbd_batch_job_item') THEN
    ALTER TABLE kbd_batch_job_item
      ADD COLUMN IF NOT EXISTS work_total_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS work_completed_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS work_failed_count integer NOT NULL DEFAULT 0;
    ALTER TABLE kbd_batch_job_item DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_item_status;
    ALTER TABLE kbd_batch_job_item ADD CONSTRAINT ck_kbd_batch_job_item_status CHECK (
      (status)::text = ANY (
        (ARRAY[
          'pending'::varchar, 'running'::varchar, 'succeeded'::varchar,
          'failed'::varchar, 'interrupted'::varchar
        ])::text[]
      )
    );
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kbd_batch_job_item_work_counts') THEN
      ALTER TABLE kbd_batch_job_item ADD CONSTRAINT ck_kbd_batch_job_item_work_counts CHECK (
        work_total_count >= 0 AND work_completed_count >= 0 AND work_failed_count >= 0
        AND work_completed_count <= work_total_count AND work_failed_count <= work_completed_count
      );
    END IF;
    DROP TRIGGER IF EXISTS update_kbd_batch_job_item_updated_at ON kbd_batch_job_item;
    CREATE TRIGGER update_kbd_batch_job_item_updated_at
        BEFORE UPDATE ON kbd_batch_job_item
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

    -- 兼容早期修复版本：曾把进程中断暂记为 failed。根据稳定错误码无损拆回 interrupted，
    -- 业务失败计数与中断计数从此独立，重复执行不会改变已经迁移的数据。
    UPDATE kbd_batch_job_item AS i
    SET status = 'interrupted',
        work_total_count = CASE WHEN j.job_type = 'reanalyze_images' THEN i.work_total_count ELSE 0 END,
        work_completed_count = CASE WHEN j.job_type = 'reanalyze_images' THEN i.work_completed_count ELSE 0 END,
        work_failed_count = CASE WHEN j.job_type = 'reanalyze_images' THEN i.work_failed_count ELSE 0 END
    FROM kbd_batch_job AS j
    WHERE i.batch_id = j.batch_id
      AND i.status IN ('failed', 'interrupted')
      AND i.error_json->>'code' = 'BATCH_PROCESS_INTERRUPTED';

    UPDATE kbd_batch_job AS j
    SET completed_count = summary.succeeded + summary.failed + summary.interrupted,
        succeeded_count = summary.succeeded,
        failed_count = summary.failed,
        interrupted_count = summary.interrupted,
        work_total_count = summary.work_total,
        work_completed_count = summary.work_completed,
        work_failed_count = summary.work_failed,
        status = 'interrupted',
        completed_at = COALESCE(j.completed_at, CURRENT_TIMESTAMP)
    FROM (
      SELECT batch_id,
             COUNT(*) FILTER (WHERE status = 'succeeded')::integer AS succeeded,
             COUNT(*) FILTER (WHERE status = 'failed')::integer AS failed,
             COUNT(*) FILTER (WHERE status = 'interrupted')::integer AS interrupted,
             COALESCE(SUM(work_total_count), 0)::integer AS work_total,
             COALESCE(SUM(work_completed_count), 0)::integer AS work_completed,
             COALESCE(SUM(work_failed_count), 0)::integer AS work_failed
      FROM kbd_batch_job_item
      GROUP BY batch_id
    ) AS summary
    WHERE j.batch_id = summary.batch_id
      AND summary.interrupted > 0;
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='sop_document') THEN
    DROP TRIGGER IF EXISTS update_sop_document_updated_at ON sop_document;
    CREATE TRIGGER update_sop_document_updated_at
        BEFORE UPDATE ON sop_document
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
  END IF;
END $$;

-- ═══════════════════════════════════════════════════════════════
-- 存量环境热修复：system_prompt.name UNIQUE 约束
--
-- 背景：desired_schema.sql 初始未声明 UNIQUE(name)，但种子文件
-- 02_system_prompts.sql 使用 ON CONFLICT (name) DO NOTHING，
-- 导致 db-seed PostSync Hook 持续失败，阻塞 ArgoCD 同步（PIT-044 类似问题）。
--
-- 修复：幂等添加约束（新环境由 desired_schema.sql 创建时自带，存量环境由此补齐）
-- ═══════════════════════════════════════════════════════════════
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='system_prompt') THEN
    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
      WHERE conname = 'system_prompt_name_key'
        AND conrelid = 'system_prompt'::regclass
    ) THEN
      ALTER TABLE system_prompt ADD CONSTRAINT system_prompt_name_key UNIQUE (name);
    END IF;
  END IF;
END $$;

-- ═══════════════════════════════════════════════════════════════
-- ReAct 工具调用历史跨轮次持久化：扩展 message_role ENUM
--
-- 背景：将 ReAct 工具调用轮次（tool_call/tool_result）持久化到 message
-- 表，使大模型在中断恢复后能完整还原推理上下文（OpenAI 规范要求）。
-- 新环境由 desired_schema.sql 的 ENUM 定义直接携带这两个值。
-- 存量环境需通过幂等 ALTER TYPE 补齐。
--
-- 注意：本文件在 Atlas apply desired_schema.sql 之前执行（函数定义阶段），
-- 此时 message_role 类型可能不存在，需先检查类型存在性再检查枚举值。
-- ═══════════════════════════════════════════════════════════════
DO $$ BEGIN
  -- 先检查 message_role 类型是否存在（Atlas apply 后才有）
  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = 'message_role') THEN
    -- 补齐 tool_call 角色（ReAct 工具调用请求，含 tool_calls JSON）
    IF NOT EXISTS (
      SELECT 1 FROM pg_enum
      WHERE enumtypid = 'message_role'::regtype
        AND enumlabel = 'tool_call'
    ) THEN
      ALTER TYPE message_role ADD VALUE IF NOT EXISTS 'tool_call';
    END IF;
    -- 补齐 tool_result 角色（工具执行结果，通过 tool_call_id 关联 tool_call）
    IF NOT EXISTS (
      SELECT 1 FROM pg_enum
      WHERE enumtypid = 'message_role'::regtype
        AND enumlabel = 'tool_result'
    ) THEN
      ALTER TYPE message_role ADD VALUE IF NOT EXISTS 'tool_result';
    END IF;
  END IF;
END $$;

-- 补齐 message 表 tool_call_id 字段（存量环境未包含此字段时自动补齐）
DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='message') THEN
    IF NOT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name = 'message' AND column_name = 'tool_call_id'
    ) THEN
      ALTER TABLE message ADD COLUMN tool_call_id text;
      COMMENT ON COLUMN message.tool_call_id IS
        'role=tool_result 时填写，关联 role=tool_call 消息中的 tool_call_id（OpenAI format），用于在恢复 ReAct 上下文时成对重建工具调用历史';
    END IF;
  END IF;
END $$;

-- ═══════════════════════════════════════════════════════════════
-- kbd_image 表：由 desired_schema.sql 创建（Atlas 管理）
--
-- 背景：KBD 分类与识图 Prompt 统一管理 + 在线重算功能需要 kbd_image 表
-- 存储 data-pipeline 抓取的原始图片二进制，供 kb-service 在线重算识图。
--
-- 设计：此表已在 desired_schema.sql 中定义（紧跟 kbd_entry 表），
-- Atlas 在 Step 2b 自动创建，无需在此处重复迁移。
-- ═══════════════════════════════════════════════════════════════
