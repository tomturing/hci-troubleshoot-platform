-- 066：日志收集壳工单 metadata 来源标记回填
--
-- 背景（2026-11-13）：
--   `Case(...)` 的 ORM 真实属性是 `metadata_`，而 case-service 建单时写的是
--   `Case(metadata=...)`——`metadata` 是 SQLAlchemy declarative 保留名，赋值只会
--   生成实例影子属性，从未落到 "case".metadata 列。结果是日志收集入口自建的壳工单
--   全部 metadata='{}'，导致：
--     1. 无法用 metadata.source == 'log_collect' 区分「壳工单」与用户真实工单；
--     2. 新上线的「认领壳工单」缺少判据，只能退化成任意工单可认领（越权）。
--   代码侧已修为 `metadata_=`（backend/case-service/app/services/case_service.py），
--   本迁移修复历史欠账。
--
-- 判定条件（三条同时成立才回填，宁可漏补不可错标）：
--   1. 标题为日志收集入口的固定格式「日志收集任务 Q…」；
--   2. 尚未带 source 标记（不覆盖任何已有 metadata）；
--   3. 该工单下没有任何会话/消息（有业务数据的工单绝不能被标成壳工单）。
--
-- 安全措施：
--   1. 幂等：只处理 `metadata -> 'source' IS NULL` 的行，可重复执行；
--   2. 行内审计：把回填时间与 trace 写进 metadata.backfill，可追溯；
--   3. 不新建表（Atlas 声明式 schema 会 DROP 额外表，见 062 的经验）。
--
-- 唯一调用链标记：log-collect-shell-case-metadata-backfill-20261113

BEGIN;

UPDATE "case" c
SET metadata = COALESCE(c.metadata, '{}'::jsonb)
        || jsonb_build_object('source', 'log_collect')
        || jsonb_build_object(
               'backfill',
               jsonb_build_object(
                   'trace', 'log-collect-shell-case-metadata-backfill-20261113',
                   'at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
               )
           ),
    updated_at = NOW()
WHERE c.title LIKE '日志收集任务 Q%'
  AND (c.metadata IS NULL OR c.metadata -> 'source' IS NULL)
  AND NOT EXISTS (SELECT 1 FROM conversation v WHERE v.case_id = c.case_id)
  AND NOT EXISTS (
      SELECT 1
      FROM message m
      JOIN conversation v ON v.conversation_id = m.conversation_id
      WHERE v.case_id = c.case_id
  );

-- 验证不变量：日志收集壳工单必须带来源标记
DO $verify$
DECLARE
    unmarked int;
BEGIN
    SELECT count(*) INTO unmarked
    FROM "case" c
    WHERE c.title LIKE '日志收集任务 Q%'
      AND (c.metadata IS NULL OR c.metadata -> 'source' IS NULL)
      AND NOT EXISTS (SELECT 1 FROM conversation v WHERE v.case_id = c.case_id);

    IF unmarked > 0 THEN
        RAISE EXCEPTION '回填失败：仍有 % 个壳工单缺少 metadata.source', unmarked;
    END IF;
    RAISE NOTICE '回填完成：壳工单来源标记齐全';
END $verify$;

COMMIT;