-- ============================================================================
-- 048_harden_extract_signals_prompt_null_and_xor.sql
-- 描述：从 Prompt 侧加固 kbd_extract_signals_v2 —— 去掉 orchestrate 输出契约里的
--       "action": "<可选>" null 诱因，并追加【输出前强制自检】（禁显式 null / 禁空占位 /
--       backend 信号 match 与 produces 二选一），降低关键信号抽取候选被 v2 契约拒收的概率。
--       version 取 2.8：高于观察到的 live staging 版本 2.7（属带外手工漂移，无迁移产出 2.6/2.7），
--       避免将存量库版本号回退。
-- 问题：KBD 16368 同类的端到端残留。PR #1162 已在服务端对 orchestrate 段做确定性 null 归一
--       （避坑 docs/verify/pitfalls/python.md V-026）；本迁移在生成侧同向收紧，属
--       「主链路+多层兜底」防御模式里的 Prompt 兜底层，减少模型直出 null / 空占位 /
--       match 与 produces 同时配置这三类必被契约拒绝的候选。
-- 影响：仅 kbd_extract_signals_v2 一行；占位符契约不变（追加文本零花括号、不新增 {name}）。
-- 幂等性：仅当仍含 "action": "<可选>" 时移除；仅当尚未含【输出前强制自检】时追加；可重复执行。
--       空库/CI 未加载该 prompt（seed 未应用）时优雅跳过，由 database/seeds/02_system_prompts.sql
--       的新环境种子提供同等加固。注意：<可选说明>（review.notes）含 <可选> 子串，
--       故 replace/verify 一律锚定完整片段 "action": "<可选>", ，不得用裸 <可选> 判断。
-- 唯一调用链：migration:20261019000001
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '开始执行 048：加固 kbd_extract_signals_v2（禁 null + match/produces 二选一）...';

    IF NOT EXISTS (SELECT 1 FROM system_prompt WHERE name = 'kbd_extract_signals_v2') THEN
        -- 空库/CI 未加载该 prompt seed：由已含加固的 database/seeds/02_system_prompts.sql 提供，优雅跳过。
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言（trace_id=migration:20261019000001）';
    ELSE
        -- 1) 去掉 orchestrate 契约里的 "action": "<可选>", null 诱因（对齐示例：无实际值即省略该键）。
        UPDATE system_prompt
        SET content_template = replace(content_template, '"action": "<可选>", ', ''),
            updated_at = NOW()
        WHERE name = 'kbd_extract_signals_v2'
          AND content_template LIKE '%"action": "<可选>", %';

        -- 2) 追加【输出前强制自检】段（幂等：不含标记才追加）。追加文本零花括号，不引入新占位符。
        UPDATE system_prompt
        SET content_template = content_template || $RULE$

# 【输出前强制自检】（null 与二选一红线，优先级最高，逐条 Candidate 发送前必须满足）
- 禁止显式 null：除「生产者信号或产出变量模式的 match 本身写 null」这一处外，任何可选字段要么给合法值、要么整键省略，严禁写成 null。重点：orchestrate 的 action、source、target、container 没有实际字符串值时就省略该键，不要写成 null，也不要写空字符串占位。
- 禁止空占位数组：只取整行时 match.extract 与 produces[].extract 只写 rows，不得附带 columns 空数组、parser=null、value_key=null；确需按列提取时 parser、非空 columns 与 value_key 必须成套出现，缺任意一个就整体省略列选择相关字段。
- backend 信号（qfk_* 且非 qfk_var）的 match 与 produces 严格二选一：判定模式配 match 且 produces 省略或为空数组；产出变量模式把 match 置 null 且 produces 非空；绝不允许 match 非空同时 produces 非空，否则违反契约被整条拒收。
- 逐条自检后再输出：确认没有残留任何 null 或空占位、match 与 produces 是否二选一、每个 extract 只含当前形态真正需要的字段；不满足就删除冗余键或改为省略，绝不带着必被契约拒绝的写法输出。
$RULE$,
            version = '2.8',
            updated_at = NOW()
        WHERE name = 'kbd_extract_signals_v2'
          AND content_template NOT LIKE '%【输出前强制自检】%';

        -- verify 门禁：行存在则必须「含【输出前强制自检】」且「已去掉 action 可选占位」，否则 fail-loud 回滚。
        IF EXISTS (
            SELECT 1 FROM system_prompt
            WHERE name = 'kbd_extract_signals_v2'
              AND (content_template NOT LIKE '%【输出前强制自检】%'
                   OR content_template LIKE '%"action": "<可选>", %')
        ) THEN
            RAISE EXCEPTION 'migration:20261019000001 未生效：kbd_extract_signals_v2 未同时满足「含【输出前强制自检】」且「去掉 action 可选占位」';
        END IF;

        RAISE NOTICE '✓ 完成 048：kbd_extract_signals_v2 已加固（trace_id=migration:20261019000001）';
    END IF;
END $$;
