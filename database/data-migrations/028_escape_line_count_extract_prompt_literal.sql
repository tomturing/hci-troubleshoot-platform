-- 修复 026 追加的 line_count Extract JSON 未转义问题。
--
-- StrictPromptLoader 使用 Python str.format 解析 Prompt；展示型 JSON 的花括号
-- 必须写成 {{ / }}，否则 "type" 会被误判为运行时占位符。026 已在存量环境
-- 执行，禁止改写历史迁移，因此通过本迁移前向修复。

UPDATE system_prompt
SET content_template = replace(
        content_template,
        $UNSAFE$match.extract={"type":"text","rows":{"mode":"all"},"cardinality":"all","source":"stdout"}$UNSAFE$,
        $SAFE$match.extract={{"type":"text","rows":{{"mode":"all"}},"cardinality":"all","source":"stdout"}}$SAFE$
    ),
    version = '2.5',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template LIKE '%match.extract={"type":"text","rows":{"mode":"all"},"cardinality":"all","source":"stdout"}%';

DO $verify$
DECLARE
    extract_prompt text;
BEGIN
    SELECT content_template INTO extract_prompt
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2';

    IF extract_prompt IS NULL THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，空库将在种子阶段写入最新版本';
    ELSIF extract_prompt LIKE '%match.extract={"type":"text","rows":{"mode":"all"},"cardinality":"all","source":"stdout"}%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 line_count Extract JSON 花括号转义未生效';
    END IF;
END
$verify$;
