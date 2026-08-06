-- 收敛 QKV acquire.keyword 与 QFK Matcher/Extract 数组语义，避免 LLM 把数组写入 qkv_*。
--
-- 仅更新动态 Prompt；历史 KBD/revision 保持不可变。该迁移必须幂等执行，兼容
-- 已执行 021/022 的存量环境以及从最新 seed 初始化的空库。

UPDATE system_prompt
SET content_template = replace(
        replace(
            content_template,
            'keyword pattern 数组中的每一项都必须能从逐字 evidence 或合法变量追溯，禁止在有证据项旁混入模型猜测项；regex pattern 必须能实际命中逐字 evidence，不能只表达意图却无法匹配自己的证据。keyword 不解释正则竖线；多关键字使用数组，正则选择使用 regex。',
            'match.pattern 数组中的每一项都必须能从逐字 evidence 或合法变量追溯，禁止在有证据项旁混入模型猜测项；regex pattern 必须能实际命中逐字 evidence，不能只表达意图却无法匹配自己的证据。keyword 不解释正则竖线；多个 QFK Matcher 关键字放在 match.pattern 数组，正则选择使用 regex。'
        ),
        '多个关键字使用数组，页面按换行编辑，逗号属于字面量。',
        'extract.rows.include/exclude 使用字符串数组做候选行过滤，页面按换行编辑，逗号属于字面量；不要把该数组写入 qkv_* 的 acquire.args.keyword。'
    ) || E'\n\n# 补充规则 27：QKV/QFK 参数类型矩阵（最高优先级）\n'
    || ' - qkv_alert/qkv_task/qkv_dialog 的 acquire.args.keyword 必须是单个非空 string，对应一次 aCLI -k 查询；多个任务动作必须拆成多条 qkv Candidate，禁止写成 keyword 数组。'
    || E'\n'
    || ' - match.pattern 可以是 string 或 string[]；extract.rows.include/exclude 使用 string[] 做候选行过滤。绝对禁止把这两类数组写入 qkv_* 的 acquire.args.keyword。',
    version = '2.3',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 27：QKV/QFK 参数类型矩阵%';

DO $verify$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM system_prompt
        WHERE name = 'kbd_extract_signals_v2'
    ) THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF NOT EXISTS (
        SELECT 1
        FROM system_prompt
        WHERE name = 'kbd_extract_signals_v2'
          AND version = '2.3'
          AND content_template LIKE '%补充规则 27：QKV/QFK 参数类型矩阵%'
          AND content_template LIKE '%acquire.args.keyword 必须是单个非空 string%'
          AND content_template LIKE '%match.pattern 可以是 string 或 string[]%'
          AND content_template LIKE '%extract.rows.include/exclude 使用 string[]%'
          AND content_template NOT LIKE '%keyword pattern 数组%'
          AND content_template NOT LIKE '%多个关键字使用数组，页面按换行编辑%'
    ) THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 QKV/QFK 参数类型矩阵升级未生效';
    END IF;
END
$verify$;
