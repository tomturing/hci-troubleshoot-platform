-- 062：失败任务 error_code 历史回填（失败必有码）
--
-- 背景（2026-11-12 staging，近 30 天）：
--   118 个失败任务里 76 个（64.4%）error_code 为空，只能靠 error_message 人肉猜原因，
--   面板无法按码聚合、告警与重试分级全部失效。代码侧已在保存边界收口
--   （LogCollectService.attribute_failure），本迁移把历史欠账一次性补齐。
--
-- 规则来源：
--   backend/case-service/app/services/log_collect_error_attribution.py 的 _RULES。
--   SQL 无法 import Python，故此处复制同一张有序规则表；两边一致性由
--   tests/unit/test_log_collect_error_attribution.py 解析本文件强校验，
--   任何一侧改动而另一侧未同步都会让 CI 失败。
--
-- 语义：
--   「没有原因」也是一种需要被看见的状态。匹配不到规则时落 LC-PLATFORM-011
--   （未归因兜底码），而不是留空——留空等于永久放弃归因。
--
-- 安全措施：
--   1. 先备份旧值到 log_collect_task_correction_20261112_errorcode（幂等）；
--   2. 只补空（error_code IS NULL），绝不覆盖上游已有的精确包装码；
--   3. detail 里写回填审计，事后可区分「上游产生的码」与「按文案推导的码」。
--
-- 唯一调用链标记：log-collect-errorcode-backfill-20261112

BEGIN;

-- Step 1: 备份（幂等）
DO $backup$
BEGIN
    IF to_regclass('public.log_collect_task_correction_20261112_errorcode') IS NULL THEN
        EXECUTE $$
            CREATE TABLE log_collect_task_correction_20261112_errorcode AS
            SELECT task_id, case_id, status, delivery_status,
                   error_code AS error_code_before,
                   error_message, delivery_error,
                   detail AS detail_snapshot,
                   NOW() AS correction_at
            FROM log_collect_task
            WHERE error_code IS NULL
              AND (status = 'failed' OR delivery_status = 'failed')
        $$;
        RAISE NOTICE '备份完成：% 条待回填', (
            SELECT count(*) FROM log_collect_task_correction_20261112_errorcode
        );
    ELSE
        RAISE NOTICE '备份表已存在，跳过创建';
    END IF;
END $backup$;

-- Step 2: 落地规则表（临时表：Step 2/3 共用，避免复制两份规则造成漂移）
-- 顺序即优先级：具体规则必须排在泛化规则之前（DISTINCT ON + ord 取最小命中）。
CREATE TEMP TABLE lc_error_rules (ord int PRIMARY KEY, needle text NOT NULL, code text NOT NULL)
ON COMMIT DROP;

INSERT INTO lc_error_rules (ord, needle, code) VALUES
        (1, '存储目录不可写', 'LC-PLATFORM-007'),
        (2, '不可写', 'LC-PLATFORM-007'),
        (3, '存储空间不足', 'LC-PLATFORM-008'),
        (4, '判定为僵尸会话已清理', 'LC-PLATFORM-013'),
        (5, '平台收尾失败', 'LC-PLATFORM-012'),
        (6, 'INTERNAL_ERROR', 'LC-PLATFORM-012'),
        (7, '服务内部错误', 'LC-PLATFORM-012'),
        (8, 'status code 5', 'LC-PLATFORM-012'),
        (9, '与 terminal_bridge 的连接已断开', 'LC-PLATFORM-010'),
        (10, 'bridge 连接已断开', 'LC-PLATFORM-010'),
        (11, '客户机还有其它交付任务', 'LC-TRANSFER-009'),
        (12, '并发容量不足', 'LC-TRANSFER-009'),
        (13, '客户机磁盘空间不足', 'LC-TRANSFER-008'),
        (14, '临时目录空间不足', 'LC-TRANSFER-008'),
        (15, '写入本地文件失败', 'LC-TRANSFER-008'),
        (16, '%TEMP%', 'LC-TRANSFER-008'),
        (17, '/sf/data/local', 'LC-ENV-004'),
        (18, '磁盘空间不足', 'LC-ENV-004'),
        (19, '可用空间', 'LC-ENV-004'),
        (20, 'SSH 会话不存在', 'LC-ENV-006'),
        (21, 'ssh_connect', 'LC-ENV-006'),
        (22, '需重新连接节点', 'LC-ENV-006'),
        (23, '重新连接 SSH', 'LC-ENV-006'),
        (24, '交付需要重新认证', 'LC-ENV-006'),
        (25, 'SSH 认证失败', 'LC-ENV-007'),
        (26, '节点离线', 'LC-ENV-008'),
        (27, 'host offline', 'LC-ENV-008'),
        (28, '日志服务器不可达', 'LC-ENV-005'),
        (29, 'acli: command not found', 'LC-EXEC-003'),
        (30, '未知的命令或者命名空间', 'LC-EXEC-003'),
        (31, 'plugin: no such file', 'LC-EXEC-003'),
        (32, '命令返回非零', 'LC-EXEC-003'),
        (33, '命令执行超时', 'LC-EXEC-002'),
        (34, '超时', 'LC-EXEC-002'),
        (35, 'SSH 等待结果失败', 'LC-EXEC-005'),
        (36, '下载安装包失败', 'LC-INSTALL-002'),
        (37, '安装包上传到 HCI 失败', 'LC-INSTALL-004'),
        (38, 'acli 安装命令失败', 'LC-INSTALL-005'),
        (39, '安装后版本验证失败', 'LC-INSTALL-006'),
        (40, '落盘校验失败', 'LC-INSTALL-007'),
        (41, '已有进行中的任务', 'LC-PLUGIN-007'),
        (42, '正在执行', 'LC-PLUGIN-007'),
        (43, '已有采集在跑', 'LC-PLUGIN-007'),
        (44, '节点上已无任务', 'LC-PLUGIN-008'),
        (45, '任务不存在', 'LC-PLUGIN-008'),
        (46, '未找到对应的日志收集任务', 'LC-PLUGIN-008'),
        (47, '采集未完成', 'LC-PLUGIN-010'),
        (48, '日志采集执行失败', 'LC-PLUGIN-010'),
        (49, '取消采集失败', 'LC-PLUGIN-011'),
        (50, '部分主机采集失败', 'LC-PLUGIN-012'),
        (51, '源包已变化', 'LC-TRANSFER-010'),
        (52, '源包仍在写入', 'LC-TRANSFER-010'),
        (53, '仍未写完', 'LC-TRANSFER-010'),
        (54, '会话已被新代替代', 'LC-TRANSFER-010'),
        (55, '上传会话大小不一致', 'LC-TRANSFER-010'),
        (56, '按新基线重建', 'LC-TRANSFER-010'),
        (57, '压缩包不存在', 'LC-TRANSFER-001'),
        (58, '源包可能已被清理', 'LC-TRANSFER-001'),
        (59, '本地副本不存在', 'LC-TRANSFER-001'),
        (60, '未找到采集产物', 'LC-TRANSFER-001'),
        (61, '校验和不一致', 'LC-TRANSFER-002'),
        (62, '上传中断', 'LC-TRANSFER-003'),
        (63, '分块上传失败', 'LC-TRANSFER-003'),
        (64, '片）', 'LC-TRANSFER-003'),
        (65, '无法确认日志包', 'LC-TRANSFER-006'),
        (66, '归档核验未通过', 'LC-TRANSFER-007'),
        (67, '直传归档核验', 'LC-TRANSFER-007'),
        (68, '归档到日志服务器失败', 'LC-TRANSFER-003');

WITH targets AS (
    SELECT
        t.task_id,
        -- 归因顺序遵守「谁失败谁出码」：采集失败看 error_message，
        -- 采集已 completed 但交付失败看 delivery_error。
        CASE
            WHEN t.status = 'failed' THEN COALESCE(t.error_message, '')
            ELSE COALESCE(t.delivery_error, t.error_message, '')
        END AS msg
    FROM log_collect_task t
    WHERE (
            t.error_code IS NULL
            -- 允许「上次回填成未归因、本次规则已能识别」的行被重新归因：
            -- 规则只会越补越细，若不能重跑，第一批历史数据会永远停在 011。
            OR (
                t.error_code = 'LC-PLATFORM-011'
                AND COALESCE(t.detail -> 'error_code_derivation' ->> 'source', '') LIKE 'backfill_062%'
            )
        )
      AND (t.status = 'failed' OR t.delivery_status = 'failed')
),
matched AS (
    -- DISTINCT ON 取 ord 最小者 = 规则表里最靠前（最具体）的命中
    SELECT DISTINCT ON (g.task_id) g.task_id, r.code
    FROM targets g
    JOIN lc_error_rules r ON strpos(g.msg, r.needle) > 0
    ORDER BY g.task_id, r.ord
)
UPDATE log_collect_task t
SET error_code = m.code,
    detail = jsonb_set(
        COALESCE(t.detail, '{}'::jsonb),
        '{error_code_derivation}',
        jsonb_build_object(
            'code', m.code,
            'source', 'backfill_062',
            'trace', 'log-collect-errorcode-backfill-20261112',
            'corrected_at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        )
    ),
    updated_at = NOW()
FROM matched m
WHERE t.task_id = m.task_id;

-- Step 3: 兜底——仍无码的失败任务落「未归因」，绝不留空
UPDATE log_collect_task t
SET error_code = 'LC-PLATFORM-011',
    detail = jsonb_set(
        COALESCE(t.detail, '{}'::jsonb),
        '{error_code_derivation}',
        jsonb_build_object(
            'code', 'LC-PLATFORM-011',
            'source', 'backfill_062_unattributed',
            'trace', 'log-collect-errorcode-backfill-20261112',
            'corrected_at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
        )
    ),
    updated_at = NOW()
WHERE (
        t.error_code IS NULL
        OR (
            t.error_code = 'LC-PLATFORM-011'
            AND COALESCE(t.detail -> 'error_code_derivation' ->> 'source', '') IN (
                'backfill_062', 'backfill_062_unattributed'
            )
        )
    )
  AND (t.status = 'failed' OR t.delivery_status = 'failed')
  -- 只处理规则仍未命中的行：已能识别的行由 Step 2 升级，不应被兜底码覆盖。
  AND NOT EXISTS (
      SELECT 1 FROM lc_error_rules r
      WHERE strpos(
          CASE
              WHEN t.status = 'failed' THEN COALESCE(t.error_message, '')
              ELSE COALESCE(t.delivery_error, t.error_message, '')
          END,
          r.needle
      ) > 0
  );

-- Step 4: 验证不变量——失败必有码
DO $verify$
DECLARE
    remaining int;
    unattributed int;
BEGIN
    SELECT count(*) INTO remaining
    FROM log_collect_task
    WHERE error_code IS NULL
      AND (status = 'failed' OR delivery_status = 'failed');

    SELECT count(*) INTO unattributed
    FROM log_collect_task
    WHERE error_code = 'LC-PLATFORM-011'
      AND (status = 'failed' OR delivery_status = 'failed');

    IF remaining > 0 THEN
        RAISE EXCEPTION '回填失败：仍有 % 个失败任务没有 error_code', remaining;
    END IF;
    RAISE NOTICE '回填完成：失败任务 error_code 空值 0 条，未归因 % 条', unattributed;
END $verify$;

COMMIT;