-- 054：修复 acquisition_args_template 类型契约（裸字符串导致采集必失败）
--
-- 背景：
--   051 整改后，SOP#2/#3/#5 的 task_logs / alert_logs 仍残留
--       "acquisition_args_template": "stdout"
--   这是原始表格列错位的遗留：真正的参数模板应为 object（{command, reason}），
--   而 "stdout" 是 output_path 的值，被串到了模板列。
--
--   051 的 COALESCE 优先级写反了（先取 acquisition_args_template 的脏值，
--   再取 expression 的正确模板），且判据是 "strategy <> tool_call"，
--   对"strategy 已正确但模板仍错"的行完全漏判。本迁移按"最终态"重做。
--
-- 运行时后果（staging 实测，conversation a4dd8df1 / SOP#3）：
--   engine 渲染模板得到 str "stdout" -> 原样作为 args 传给 CompositeToolExecutor
--   -> app/main.py:539 `args.get("node_ip")` 抛
--      AttributeError: 'str' object has no attribute 'get'
--   -> 被 engine 捕获后降级 _request_user_input 弹卡，
--      而 LLM 每重试一次 sop_request_variable 就再弹一张 -> 同一轮重复下发变量卡。
--
-- 修复依据（唯一事实源 = SOP 正文 prerequisites 里的前置检查命令，不臆造）：
--   SOP#3 task_logs  : acli --formatter json task get -k "启动虚拟机" -s 'failed'
--   SOP#2 alert_logs : acli --formatter json alert get  -k "磁盘"
--   SOP#5 alert_logs : acli --formatter json alert get  -k "内存"
--   SOP#2 task_logs  : 正文无对应采集命令且已 deprecated -> 只清除非法模板，不再采集
--
-- 唯一调用链标记：sop_variable_acquisition_args_contract_v1
-- 幂等：仅覆盖"非 object"的非法模板，合法配置不覆盖，可重复执行。
--
-- 回滚：
--   UPDATE sop_document SET variable_schema = b.variable_schema, updated_at = b.updated_at
--   FROM _backup_20261026_sop_var b WHERE sop_document.id = b.id;

BEGIN;

-- ──────────────────────────────────────────────────────────────────────────────
-- 1. 活跃日志变量：按正文命令归位为合法 object 模板
-- ──────────────────────────────────────────────────────────────────────────────
WITH fix(sop_id, var_name, tmpl) AS (
    VALUES
        (2, 'alert_logs', jsonb_build_object(
            'command', 'acli --formatter json alert get -k "磁盘"',
            'reason',  '获取所有“磁盘”相关告警原始数据'
        )),
        (3, 'task_logs', jsonb_build_object(
            'command', 'acli --formatter json task get -k "启动虚拟机" -s ''failed''',
            'reason',  '获取所有“虚拟机开机”相关失败任务原始数据'
        )),
        (5, 'alert_logs', jsonb_build_object(
            'command', 'acli --formatter json alert get -k "内存"',
            'reason',  '获取所有“内存”相关告警原始数据'
        ))
)
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN f.var_name IS NOT NULL
                     AND jsonb_typeof(item.v -> 'acquisition_args_template') <> 'object'
                THEN item.v || jsonb_build_object('acquisition_args_template', f.tmpl)
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
        LEFT JOIN fix f
               ON f.sop_id = d.id
              AND f.var_name = item.v ->> 'name'
    ),
    updated_at = NOW()
WHERE d.id IN (2, 3, 5)
  AND d.variable_schema IS NOT NULL;

-- ──────────────────────────────────────────────────────────────────────────────
-- 2. deprecated 变量：清除非法采集模板，避免被误采集时再次崩在类型上
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN COALESCE((item.v ->> 'deprecated')::boolean, FALSE)
                     AND item.v ? 'acquisition_args_template'
                     AND jsonb_typeof(item.v -> 'acquisition_args_template') <> 'object'
                THEN item.v - 'acquisition_args_template'
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
WHERE d.variable_schema IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE COALESCE((v ->> 'deprecated')::boolean, FALSE)
        AND v ? 'acquisition_args_template'
        AND jsonb_typeof(v -> 'acquisition_args_template') <> 'object'
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- 3. SOP#5 alert_logs 描述漂移：正文查的是"内存"，描述却写"磁盘"
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN item.v ->> 'name' = 'alert_logs'
                THEN item.v || jsonb_build_object(
                        'description', '内存相关告警原始数据（acli alert get 的 JSON 输出）'
                     )
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
WHERE d.id = 5
  AND d.variable_schema @> '[{"name": "alert_logs"}]'::jsonb;

-- ──────────────────────────────────────────────────────────────────────────────
-- 4. 收敛断言：活跃 tool_call 变量的模板不得为非 object 的标量
--    注意 null 值放行（engine 侧 `or {}` 会兜底为 {}，不会崩在类型上）
-- ──────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE
    bad TEXT;
BEGIN
    SELECT string_agg(d.id || '/' || (v ->> 'name'), ', ')
      INTO bad
      FROM sop_document d, jsonb_array_elements(d.variable_schema) v
     WHERE v ->> 'acquisition_strategy' = 'tool_call'
       AND COALESCE((v ->> 'deprecated')::boolean, FALSE) = FALSE
       AND v ? 'acquisition_args_template'
       AND jsonb_typeof(v -> 'acquisition_args_template') NOT IN ('object', 'null');

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION '054 未收敛：以下活跃变量的 acquisition_args_template 非 object -> %', bad;
    END IF;
END $$;

COMMIT;
