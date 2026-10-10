-- 修复 F：SOP「硬盘坏道」(id=4) 存量数据对齐与链路根变量兜底声明。
--
-- 背景（Q2026100812343 复盘，V-017）：SOP#4 的 tree_json 变量 source 与已订正的
-- variable_schema 长期不一致——tree_json 残留中文别名 "工具调用"（解析后为 tool_call 且无
-- acquisition_tool），而 variable_schema 已把 alert_logs 订正为 tool_call/acli_exec、
-- date 订正为 skill_call/hci-alert-parsing。二者错位会误导编排层与复盘。
--
-- 本迁移：
--   1) 以 variable_schema 为唯一事实源，重编译 tree_json 各变量的 source（tool:/skill:/env:/
--      json_extract/user_input/user_confirm），彻底消除残留 "工具调用" 中文别名；
--   2) 为链路根/关键变量（alert_logs/node_ip/asan_disks/disk_dev）显式声明
--      fallback_strategy="user_input"，与「采集失败一律转人工弹框」策略一致。
--
-- 唯一调用链标记：sop_disk_badtrack_align_v1（幂等：仅当仍存在 "工具调用" 源时才改写 source）。
-- 采集镜像 jq 的 ONIGURUMA 缺陷：SOP#4 命令模板已采用 `acli ... alert get -k 磁盘` 关键字下推，
-- 未使用 jq test()/match()，故无需改写命令；若后续新增含 test()/match() 的采集模板，应继续下推为 -k 粗筛。

-- ── 1) 以 variable_schema 重编译 tree_json 变量 source ────────────────────────────
UPDATE sop_document
SET tree_json = jsonb_set(
        tree_json::jsonb,
        '{variables}',
        (
            SELECT COALESCE(jsonb_agg(new_v ORDER BY ord), '[]'::jsonb)
            FROM (
                SELECT
                    ord,
                    jsonb_set(
                        v,
                        '{source}',
                        COALESCE(
                            (
                                SELECT CASE
                                    WHEN s.acquisition_strategy = 'tool_call'
                                        THEN to_jsonb('tool:' || COALESCE(s.acquisition_tool, ''))
                                    WHEN s.acquisition_strategy = 'skill_call'
                                        THEN to_jsonb('skill:' || COALESCE(s.acquisition_tool, ''))
                                    WHEN s.acquisition_strategy = 'env_injection'
                                        THEN to_jsonb('env:' || COALESCE(s.acquisition_tool, ''))
                                    WHEN s.acquisition_strategy = 'json_extract' THEN '"json_extract"'::jsonb
                                    WHEN s.acquisition_strategy = 'user_input' THEN '"user_input"'::jsonb
                                    WHEN s.acquisition_strategy = 'user_confirm' THEN '"user_confirm"'::jsonb
                                    ELSE v -> 'source'
                                END
                                FROM (
                                    SELECT
                                        elem ->> 'acquisition_strategy' AS acquisition_strategy,
                                        elem ->> 'acquisition_tool'     AS acquisition_tool
                                    FROM jsonb_array_elements(variable_schema) elem
                                    WHERE elem ->> 'name' = v ->> 'name'
                                    LIMIT 1
                                ) s
                            ),
                            v -> 'source'
                        )
                    ) AS new_v
                FROM jsonb_array_elements(tree_json -> 'variables') WITH ORDINALITY AS t(v, ord)
            ) x
        ),
        true
    ),
    updated_at = NOW()
WHERE id = 4
  AND title = '硬盘坏道'
  AND EXISTS (
        SELECT 1
        FROM jsonb_array_elements(tree_json -> 'variables') e
        WHERE e ->> 'source' IN ('工具调用', '工具')
    );

-- ── 2) 链路根/关键变量显式声明 fallback_strategy="user_input" ──────────────────────
UPDATE sop_document
SET variable_schema = (
        SELECT COALESCE(jsonb_agg(
                   CASE
                       WHEN v ->> 'name' IN ('alert_logs', 'node_ip', 'asan_disks', 'disk_dev')
                            AND COALESCE(v ->> 'fallback_strategy', '') <> 'user_input'
                           THEN jsonb_set(v, '{fallback_strategy}', '"user_input"'::jsonb, true)
                       ELSE v
                   END
                   ORDER BY ord), '[]'::jsonb)
        FROM jsonb_array_elements(variable_schema) WITH ORDINALITY AS t(v, ord)
    ),
    updated_at = NOW()
WHERE id = 4
  AND title = '硬盘坏道';

-- ── 校验：唯一调用链标记 sop_disk_badtrack_align_v1 ────────────────────────────────
DO $verify$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sop_document WHERE id = 4 AND title = '硬盘坏道') THEN
        RAISE NOTICE '未找到 SOP「硬盘坏道」(id=4)，跳过对齐断言（空库或未导入该 SOP）';
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM sop_document,
             jsonb_array_elements(tree_json -> 'variables') e
        WHERE id = 4 AND title = '硬盘坏道'
          AND e ->> 'source' IN ('工具调用', '工具')
    ) THEN
        RAISE EXCEPTION 'sop_disk_badtrack_align_v1 未生效：tree_json 仍残留中文别名 source';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM sop_document,
             jsonb_array_elements(tree_json -> 'variables') e
        WHERE id = 4 AND title = '硬盘坏道'
          AND e ->> 'name' = 'alert_logs' AND e ->> 'source' = 'tool:acli_exec'
    ) THEN
        RAISE EXCEPTION 'sop_disk_badtrack_align_v1 未生效：alert_logs source 未对齐为 tool:acli_exec';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM sop_document,
             jsonb_array_elements(tree_json -> 'variables') e
        WHERE id = 4 AND title = '硬盘坏道'
          AND e ->> 'name' = 'date' AND e ->> 'source' = 'skill:hci-alert-parsing'
    ) THEN
        RAISE EXCEPTION 'sop_disk_badtrack_align_v1 未生效：date source 未对齐为 skill:hci-alert-parsing';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM sop_document,
             jsonb_array_elements(variable_schema) e
        WHERE id = 4 AND title = '硬盘坏道'
          AND e ->> 'name' IN ('alert_logs', 'node_ip', 'asan_disks', 'disk_dev')
          AND COALESCE(e ->> 'fallback_strategy', '') <> 'user_input'
    ) THEN
        RAISE EXCEPTION 'sop_disk_badtrack_align_v1 未生效：链路根变量 fallback_strategy 未声明为 user_input';
    END IF;

    RAISE NOTICE 'sop_disk_badtrack_align_v1 校验通过：tree_json source 已对齐，根变量兜底已声明';
END
$verify$;
