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
