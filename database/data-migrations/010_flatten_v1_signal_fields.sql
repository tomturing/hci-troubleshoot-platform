-- ===========================================================================
-- Migration: 010_flatten_v1_signal_fields.sql
-- 说明: 将历史 signals_json 中的 v1 残留字段拍平/改名为 v2 规范名：
--       1) acquire.args.target.{scope,resource,path,time_window}
--          -> 顶层 host(除 qfk_service) / file(仅 qfk_log) / container(仅 qfk_service)
--             / path(仅 qfk_log) / time_window(仅 qfk_log)
--             / resource_keyword(其余 qfk 工具)
--          （v1 的 acquirer_args.target 嵌套对象已废弃，v2 为扁平字段；按工具契约
--            additionalProperties:false 分支落位，避免产生幽灵字段导致后续保存 422）
--       2) acquire.args.sub_command -> command
--       3) acquire.args.description  -> instruction
--       4) acquire.args.keyword（QFK 历史别名）-> resource_keyword
--          （仅 qfk_* 工具；qkv 的 keyword 是合法采集关键词字段，不动）
--       5) 删除顶层 _v1_legacy 字段
-- 结构: signals_json 现为 v2 对象 {schema_version, signals}；本迁移兼容历史裸数组形态，
--       统一写回 v2 对象包装。
-- 幂等: 对已是 v2 扁平名的信号无副作用（仅处理 target/sub_command/description/keyword/
--       _v1_legacy；二次执行结构不变）。
-- 范围: kbd_entry、sop_document 两张表的 signals_json。
-- ===========================================================================

CREATE OR REPLACE FUNCTION _flatten_v1_signal_fields(sig jsonb) RETURNS jsonb AS $$
DECLARE
    args    jsonb;
    tool    text;
    t       jsonb;
    new_args jsonb := '{}'::jsonb;
    k       text;
    v       jsonb;
    has_rk  boolean;
BEGIN
    tool := sig->'acquire'->>'tool';
    args := COALESCE(sig->'acquire'->'args', '{}'::jsonb);

    -- 是否已存在 resource_keyword（决定 keyword 别名是归一还是丢弃）
    has_rk := args ? 'resource_keyword';

    -- 遍历原 args，逐键处理（target/sub_command/description/keyword 特殊对待）
    FOR k, v IN SELECT * FROM jsonb_each(args) LOOP
        IF k = 'target' THEN
            t := v;
            IF jsonb_typeof(t) = 'object' THEN
                -- host：除 qfk_service 外的 qfk 工具允许（qfk_service 契约无 host；qkv 无 target）
                IF t ? 'scope' AND t->>'scope' IS NOT NULL
                   AND tool LIKE 'qfk_%' AND tool <> 'qfk_service' THEN
                    new_args := jsonb_set(new_args, '{host}', t->'scope');
                END IF;
                -- path / time_window：仅 qfk_log 契约允许
                IF tool = 'qfk_log' THEN
                    IF t ? 'path' AND t->>'path' IS NOT NULL THEN
                        new_args := jsonb_set(new_args, '{path}', t->'path');
                    END IF;
                    IF t ? 'time_window' AND t->>'time_window' IS NOT NULL THEN
                        new_args := jsonb_set(new_args, '{time_window}', t->'time_window');
                    END IF;
                END IF;
                -- resource 按工具语义落位
                IF t ? 'resource' AND t->>'resource' IS NOT NULL THEN
                    IF tool = 'qfk_log' THEN
                        new_args := jsonb_set(new_args, '{file}', t->'resource');
                    ELSIF tool = 'qfk_service' THEN
                        new_args := jsonb_set(new_args, '{container}', t->'resource');
                    ELSIF tool LIKE 'qfk_%' THEN
                        -- 其余 qfk 工具落位 resource_keyword（契约可选字段），避免数据丢失
                        new_args := jsonb_set(new_args, '{resource_keyword}', t->'resource');
                        has_rk := true;
                    END IF;
                END IF;
            END IF;
        ELSIF k = 'sub_command' THEN
            IF v IS NOT NULL THEN
                new_args := jsonb_set(new_args, '{command}', v);
            END IF;
        ELSIF k = 'description' THEN
            IF v IS NOT NULL THEN
                new_args := jsonb_set(new_args, '{instruction}', v);
            END IF;
        ELSIF k = 'keyword' AND tool LIKE 'qfk_%' THEN
            -- QFK 的 keyword 历史别名：归一为 resource_keyword；若已有 resource_keyword 则丢弃别名
            -- （qfk 契约无 keyword 字段，绝不能原样保留以免 422；qkv 的 keyword 走 ELSE 原样保留）
            IF v IS NOT NULL AND NOT has_rk THEN
                new_args := jsonb_set(new_args, '{resource_keyword}', v);
                has_rk := true;
            END IF;
        ELSE
            new_args := jsonb_set(new_args, ARRAY[k], v);
        END IF;
    END LOOP;

    sig := jsonb_set(sig, '{acquire,args}', new_args);
    sig := sig - '_v1_legacy';
    RETURN sig;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    r       RECORD;
    sigs    jsonb;   -- 从 v2 对象提取或历史裸数组
    new_arr jsonb;
    new_doc jsonb;
    tbl     text;
    jtype   text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['kbd_entry', 'sop_document'] LOOP
        FOR r IN EXECUTE format(
            'SELECT id, signals_json FROM %I '
            'WHERE signals_json IS NOT NULL '
            'AND signals_json <> ''[]''::jsonb AND signals_json <> ''{}''::jsonb', tbl
        ) LOOP
            jtype := jsonb_typeof(r.signals_json);
            IF jtype = 'object' THEN
                -- v2 文档对象 {schema_version, signals}
                sigs := COALESCE(r.signals_json->'signals', '[]'::jsonb);
            ELSIF jtype = 'array' THEN
                -- 历史裸数组（v1 残留形态）
                sigs := r.signals_json;
            ELSE
                -- 非对象非数组（异常形态），跳过不动避免破坏
                CONTINUE;
            END IF;

            SELECT jsonb_agg(_flatten_v1_signal_fields(s))
              INTO new_arr
              FROM jsonb_array_elements(sigs) AS s;

            -- 统一写回 v2 对象包装（无论原形态，规范化为 {schema_version, signals}）
            new_doc := jsonb_build_object('schema_version', 2, 'signals', COALESCE(new_arr, '[]'::jsonb));
            EXECUTE format('UPDATE %I SET signals_json = $1 WHERE id = $2', tbl)
              USING new_doc, r.id;
        END LOOP;
    END LOOP;
END $$;

DROP FUNCTION IF EXISTS _flatten_v1_signal_fields(jsonb);
