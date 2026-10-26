-- 053：为 SOP 变量补 display_name（卡片可读性）
--
-- 背景：变量名受标识符约束（^[a-z][a-z0-9_]{0,63}$），像 qemu_log_path 直接
-- 展示给用户可读性很差。契约已支持 display_name，前端卡片按
--   display_name -> description 首句 -> variable_name
-- 依次兜底；本迁移把存量变量的 display_name 一次性补齐，
-- 让"命名自由"不以牺牲用户体验为代价。
--
-- 唯一调用链标记：sop_variable_display_name_v1
-- 幂等：仅填充 display_name 为空的变量；已有显式展示名不覆盖。
--
-- 截断规则：取 description 的第一句（按全角括号 / 半角括号 / 分号 / 句号切分）。
-- 例：
--   "QEMU 日志路径（由 task_day + vmid 推导）" -> "QEMU 日志路径"
--   "证书文件绝对路径；用于 acli system md5sum ..." -> "证书文件绝对路径"

BEGIN;

-- ── 1. sop_document.variable_schema ─────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN (
                        COALESCE(item.v ->> 'display_name', '') = ''
                        OR item.v ->> 'display_name' = item.v ->> 'name'
                     )
                     AND COALESCE(item.v ->> 'description', '') <> ''
                THEN item.v || jsonb_build_object(
                        'display_name',
                        trim(both ' ' FROM
                            split_part(
                                split_part(
                                    split_part(
                                        split_part(item.v ->> 'description', '（', 1),
                                    '(', 1),
                                '；', 1),
                            '。', 1)
                        )
                     )
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
WHERE d.id IN (1, 2, 3, 4, 5)
  AND d.variable_schema IS NOT NULL
  AND jsonb_typeof(d.variable_schema) = 'array'
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE (COALESCE(v ->> 'display_name', '') = '' OR v ->> 'display_name' = v ->> 'name')
        AND COALESCE(v ->> 'description', '') <> ''
  );

-- ── 2. sop_variable_pack.variables（同步展示名，并升版本触发漂移检测）──────
UPDATE sop_variable_pack p
SET variables = (
        SELECT jsonb_agg(
            CASE
                WHEN (
                        COALESCE(item.v ->> 'display_name', '') = ''
                        OR item.v ->> 'display_name' = item.v ->> 'name'
                     )
                     AND COALESCE(item.v ->> 'description', '') <> ''
                THEN item.v || jsonb_build_object(
                        'display_name',
                        trim(both ' ' FROM
                            split_part(
                                split_part(
                                    split_part(
                                        split_part(item.v ->> 'description', '（', 1),
                                    '(', 1),
                                '；', 1),
                            '。', 1)
                        )
                     )
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(p.variables) WITH ORDINALITY AS item(v, ord)
    ),
    version = p.version + 1,
    updated_at = NOW()
WHERE p.pack_name = 'hci_common_vt_diag'
  AND p.version = 1
  AND jsonb_typeof(p.variables) = 'array';

-- applied_packs 的 version 同步到包当前版本，避免刚升版就全量漂移
UPDATE sop_document d
SET applied_packs = (
        SELECT jsonb_agg(
            CASE
                WHEN item.v ->> 'pack' = p.pack_name
                THEN jsonb_build_object('pack', p.pack_name, 'version', p.version)
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.applied_packs) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
FROM sop_variable_pack p
WHERE p.pack_name = 'hci_common_vt_diag'
  AND d.applied_packs @> jsonb_build_array(jsonb_build_object('pack', 'hci_common_vt_diag'))
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.applied_packs) v
      WHERE v ->> 'pack' = 'hci_common_vt_diag' AND (v ->> 'version')::int <> p.version
  );

COMMIT;
