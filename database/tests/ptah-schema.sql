-- 验证扩展、应用函数、向量索引及关键业务行为；完整期望状态由 schema diff 验证。
BEGIN;
DO $$
DECLARE
  owner_id uuid;
  conversation_key uuid;
  message_key uuid;
  case_number varchar;
  next_case_number varchar;
  actual integer;
  missing_objects text;
  default_sql text;
  default_is_null boolean;
BEGIN
  -- 只验证所需对象，不限制后续 PR 新增表、函数或触发器；完整期望状态另由 schema diff 验证。
  SELECT string_agg(name, ', ') INTO missing_objects FROM unnest(ARRAY[
    'public.update_updated_at_column()', 'public.fn_update_conversation_message_count()',
    'public.generate_case_id()', 'public.update_bundle_metadata_updated_at()'
  ]) AS expected(name) WHERE to_regprocedure(name) IS NULL;
  IF missing_objects IS NOT NULL THEN RAISE EXCEPTION '缺少应用函数: %', missing_objects; END IF;
  SELECT count(*) INTO actual FROM pg_extension
    WHERE extname IN ('vector','pgcrypto','uuid-ossp','pg_trgm');
  IF actual <> 4 THEN RAISE EXCEPTION '缺少声明的扩展'; END IF;
  SELECT count(*) INTO actual FROM pg_class c JOIN pg_index i ON i.indexrelid=c.oid
    JOIN pg_am a ON a.oid=c.relam
    WHERE c.relname IN ('idx_kb_category_embedding','idx_kbd_entry_embedding')
      AND a.amname='ivfflat' AND c.reloptions @> ARRAY['lists=100'] AND i.indisvalid;
  IF actual <> 2 THEN RAISE EXCEPTION 'IVFFlat 索引定义未保留'; END IF;
  IF '[1,0,0]'::vector <=> '[1,0,0]'::vector <> 0 THEN
    RAISE EXCEPTION 'pgvector 余弦距离验证失败';
  END IF;
  SELECT column_default INTO default_sql FROM information_schema.columns
    WHERE table_schema='public' AND table_name='sop_execution'
      AND column_name='pending_variable_name';
  IF default_sql IS NOT NULL THEN
    EXECUTE 'SELECT (' || default_sql || ') IS NULL' INTO default_is_null;
    IF NOT default_is_null THEN
      RAISE EXCEPTION 'pending_variable_name 必须保留 SQL NULL 默认值';
    END IF;
  END IF;

  INSERT INTO "user"(client_id,updated_at) VALUES ('ptah-functional-test','2000-01-01')
    RETURNING user_id INTO owner_id;
  UPDATE "user" SET username='trigger-test' WHERE user_id=owner_id;
  IF (SELECT updated_at FROM "user" WHERE user_id=owner_id) <> CURRENT_TIMESTAMP THEN
    RAISE EXCEPTION 'updated_at 触发器未执行';
  END IF;
  case_number := generate_case_id();
  IF case_number !~ '^Q[0-9]{13}$' THEN RAISE EXCEPTION '生成的工单编号格式错误'; END IF;
  INSERT INTO "case"(case_id,user_id,client_id,title)
    VALUES(case_number,owner_id,'ptah-functional-test','Schema 行为测试');
  next_case_number := generate_case_id();
  IF next_case_number=case_number THEN RAISE EXCEPTION '工单编号未递增'; END IF;
  INSERT INTO conversation(case_id) VALUES(case_number) RETURNING conversation_id INTO conversation_key;
  INSERT INTO message(conversation_id,case_id,role,content)
    VALUES(conversation_key,case_number,'user','消息计数测试') RETURNING message_id INTO message_key;
  IF (SELECT message_count FROM conversation WHERE conversation_id=conversation_key) <> 1 THEN
    RAISE EXCEPTION '消息 INSERT 未准确计数一次';
  END IF;
  DELETE FROM message WHERE message_id=message_key;
  IF (SELECT message_count FROM conversation WHERE conversation_id=conversation_key) <> 0 THEN
    RAISE EXCEPTION '消息 DELETE 未恢复计数';
  END IF;
END $$;
ROLLBACK;
