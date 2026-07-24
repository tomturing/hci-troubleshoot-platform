-- ===========================================================================
-- Migration: 010_flatten_v1_signal_fields.sql
-- 说明: 将历史 signals_json 中的 v1 残留字段拍平/改名为 v2 规范名：
--       1) acquire.args.target.{scope,resource,path,time_window}
--          → 顶层 host / file(仅 qfk_log) / container(仅 qfk_service) / path / time_window
--          （v1 的 acquirer_args.target 嵌套对象已废弃，v2 为扁平 host/resource/path/time_window）
--       2) acquire.args.sub_command → command
--       3) acquire.args.description  → instruction
--       4) 删除顶层 _v1_legacy 字段
-- 幂等: 对已是 v2 扁平名的信号无副作用（仅处理 target/sub_command/description）。
-- 范围: kbd_entry、sop_document 两张表的 signals_json（jsonb 数组）。
-- ===========================================================================

CREATE OR REPLACE FUNCTION _flatten_v1_signal_fields(sig jsonb) RETURNS jsonb AS $$
DECLARE
    args    jsonb;
    tool    text;
    t       jsonb;
    new_args jsonb := '{}'::jsonb;
    k       text;
    v       jsonb;
BEGIN
    tool := sig->'acquire'->>'tool';
    args := COALESCE(sig->'acquire'->'args', '{}'::jsonb);

    -- 遍历原 args，逐键处理（target/sub_command/description 特殊对待）
    FOR k, v IN SELECT * FROM jsonb_each(args) LOOP
        IF k = 'target' THEN
            t := v;
            IF jsonb_typeof(t) = 'object' THEN
                IF t ? 'scope' AND t->>'scope' IS NOT NULL THEN
                    new_args := jsonb_set(new_args, '{host}', t->'scope');
                END IF;
                IF t ? 'path' AND t->>'path' IS NOT NULL THEN
                    new_args := jsonb_set(new_args, '{path}', t->'path');
                END IF;
                IF t ? 'time_window' AND t->>'time_window' IS NOT NULL THEN
                    new_args := jsonb_set(new_args, '{time_window}', t->'time_window');
                END IF;
                IF t ? 'resource' AND t->>'resource' IS NOT NULL THEN
                    -- resource 按工具语义落位：log→file(日志文件名)，service→container(组)
                    IF tool = 'qfk_log' THEN
                        new_args := jsonb_set(new_args, '{file}', t->'resource');
                    ELSIF tool = 'qfk_service' THEN
                        new_args := jsonb_set(new_args, '{container}', t->'resource');
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
        ELSE
            new_args := jsonb_set(new_args, ARRAY[k], v);
        END IF;
    END LOOP;

    -- 兜底：若无 command 但原 args 有 sub_command 已被处理；instruction 已处理
    sig := jsonb_set(sig, '{acquire,args}', new_args);
    sig := sig - '_v1_legacy';
    RETURN sig;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    r       RECORD;
    new_arr jsonb;
    tbl     text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['kbd_entry', 'sop_document'] LOOP
        FOR r IN EXECUTE format(
            'SELECT id, signals_json FROM %I WHERE signals_json IS NOT NULL AND signals_json != ''[]''::jsonb', tbl
        ) LOOP
            SELECT jsonb_agg(_flatten_v1_signal_fields(s))
              INTO new_arr
              FROM jsonb_array_elements(r.signals_json) AS s;
            EXECUTE format('UPDATE %I SET signals_json = $1 WHERE id = $2', tbl)
              USING COALESCE(new_arr, '[]'::jsonb), r.id;
        END LOOP;
    END LOOP;
END $$;

DROP FUNCTION IF EXISTS _flatten_v1_signal_fields(jsonb);
