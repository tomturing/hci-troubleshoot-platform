-- 将原 desired_extras.sql 中的遗留修复移入版本化数据迁移。
-- 新库已在 desired_schema.sql 中声明相关对象；只扩展尚缺旧版本标记的 CHECK，
-- 不覆盖完整或后续扩展的约束，避免 041+ 数据迁移使用新值时被旧列表阻断。
-- 存量库仍需在 Schema 收敛前完成中断任务的数据转换。

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

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='kbd_batch_job') THEN
    ALTER TABLE kbd_batch_job
      ADD COLUMN IF NOT EXISTS work_total_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS work_completed_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS work_failed_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS interrupted_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS retry_of_batch_id uuid,
      ADD COLUMN IF NOT EXISTS request_json jsonb NOT NULL DEFAULT '{}'::jsonb;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_kbd_batch_job_retry_of'
      AND conrelid = 'public.kbd_batch_job'::regclass) THEN
      ALTER TABLE kbd_batch_job ADD CONSTRAINT fk_kbd_batch_job_retry_of
        FOREIGN KEY (retry_of_batch_id) REFERENCES kbd_batch_job (batch_id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_kbd_batch_job_retry_of'
      AND conrelid = 'public.kbd_batch_job'::regclass) THEN
      ALTER TABLE kbd_batch_job ADD CONSTRAINT uq_kbd_batch_job_retry_of UNIQUE (retry_of_batch_id);
    END IF;
    CREATE INDEX IF NOT EXISTS idx_kbd_batch_job_retry_of
      ON kbd_batch_job (retry_of_batch_id) WHERE retry_of_batch_id IS NOT NULL;
    IF EXISTS (SELECT 1 FROM pg_constraint
      WHERE conrelid = 'public.kbd_batch_job'::regclass AND conname = 'ck_kbd_batch_job_status'
        AND position('''interrupted''' IN pg_get_constraintdef(oid)) = 0) THEN
      ALTER TABLE kbd_batch_job DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_status;
      ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_status CHECK (
        (status)::text = ANY (
          (ARRAY[
            'pending'::varchar, 'running'::varchar, 'completed'::varchar,
            'partial_failed'::varchar, 'failed'::varchar, 'interrupted'::varchar
          ])::text[]
        )
      );
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint
      WHERE conrelid = 'public.kbd_batch_job'::regclass AND conname = 'ck_kbd_batch_job_type'
        AND position('''extract_signals''' IN pg_get_constraintdef(oid)) = 0) THEN
      ALTER TABLE kbd_batch_job DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_type;
      ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_type CHECK (
        (job_type)::text = ANY (
          (ARRAY[
            'reanalyze_images'::varchar, 'reclassify'::varchar, 'extract_signals'::varchar,
            'approve'::varchar, 'reject'::varchar
          ])::text[]
        )
      );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kbd_batch_job_request_json'
      AND conrelid = 'public.kbd_batch_job'::regclass) THEN
      ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_request_json
        CHECK (jsonb_typeof(request_json) = 'object');
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint
      WHERE conrelid = 'public.kbd_batch_job'::regclass AND conname = 'ck_kbd_batch_job_counts'
        AND position('interrupted_count' IN pg_get_constraintdef(oid)) = 0) THEN
      ALTER TABLE kbd_batch_job DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_counts;
      ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_counts CHECK (
        total_count > 0 AND completed_count >= 0 AND succeeded_count >= 0
        AND failed_count >= 0 AND interrupted_count >= 0
        AND completed_count = succeeded_count + failed_count + interrupted_count
        AND completed_count <= total_count
      );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kbd_batch_job_work_counts'
      AND conrelid = 'public.kbd_batch_job'::regclass) THEN
      ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_work_counts CHECK (
        work_total_count >= 0 AND work_completed_count >= 0 AND work_failed_count >= 0
        AND work_completed_count <= work_total_count AND work_failed_count <= work_completed_count
      );
    END IF;
  END IF;

  IF EXISTS (SELECT FROM pg_tables WHERE schemaname='public' AND tablename='kbd_batch_job_item') THEN
    ALTER TABLE kbd_batch_job_item
      ADD COLUMN IF NOT EXISTS work_total_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS work_completed_count integer NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS work_failed_count integer NOT NULL DEFAULT 0;
    IF EXISTS (SELECT 1 FROM pg_constraint
      WHERE conrelid = 'public.kbd_batch_job_item'::regclass AND conname = 'ck_kbd_batch_job_item_status'
        AND position('''interrupted''' IN pg_get_constraintdef(oid)) = 0) THEN
      ALTER TABLE kbd_batch_job_item DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_item_status;
      ALTER TABLE kbd_batch_job_item ADD CONSTRAINT ck_kbd_batch_job_item_status CHECK (
        (status)::text = ANY (
          (ARRAY[
            'pending'::varchar, 'running'::varchar, 'succeeded'::varchar,
            'failed'::varchar, 'interrupted'::varchar
          ])::text[]
        )
      );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_kbd_batch_job_item_work_counts'
      AND conrelid = 'public.kbd_batch_job_item'::regclass) THEN
      ALTER TABLE kbd_batch_job_item ADD CONSTRAINT ck_kbd_batch_job_item_work_counts CHECK (
        work_total_count >= 0 AND work_completed_count >= 0 AND work_failed_count >= 0
        AND work_completed_count <= work_total_count AND work_failed_count <= work_completed_count
      );
    END IF;

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

-- Schema 收敛前保留存量对话历史中的工具调用角色。
-- 新库的 desired_schema.sql 已声明这些枚举值。
-- 未完成初始化的遗留环境可能尚无该枚举。
DO $$ BEGIN
  -- 仅在存量环境已声明该枚举时补齐枚举值。
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
      WHERE table_schema = 'public' AND table_name = 'message' AND column_name = 'tool_call_id'
    ) THEN
      ALTER TABLE message ADD COLUMN tool_call_id text;
      COMMENT ON COLUMN message.tool_call_id IS
        'role=tool_result 时填写，关联 role=tool_call 消息中的 tool_call_id（OpenAI format），用于在恢复 ReAct 上下文时成对重建工具调用历史';
    END IF;
  END IF;
END $$;
