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
