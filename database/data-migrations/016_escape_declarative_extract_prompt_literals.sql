-- 修复已执行 015 的运行环境：StrictPromptLoader 使用 Python str.format，
-- Prompt 中作为示例展示的 JSON 花括号必须转义为 {{ / }}，否则会被误判为运行时占位符。
-- 幂等：仅替换 015 追加的三条规则文本；不会变更案例或历史 revision。

UPDATE system_prompt
SET content_template = replace(
        content_template,
        $UNSAFE$
- 行选择：按关键字时使用 rows={"mode":"keywords","include":[...],"exclude":[...],"include_mode":"all|any","case_sensitive":true}；按行号时使用 rows={"mode":"indices","basis":"data","indices":[...],"ranges":[...]}；所有行使用 rows={"mode":"all"}。
- 列选择：整行不配置 columns；空白分列使用 parser="whitespace_table"；单字符分隔使用 parser="delimited_table" 与 delimiter。每一列使用 {"key":"稳定大写名","selector":{"by":"index","index":N}|{"by":"header","name":"列名","aliases":[...]},"value_mode":"string|integer|number|boolean"}；需要标量值时 value_key 必须等于其中一个 key。
- 示例：{"name":"PID","type":"integer","extract":{"type":"text","parser":"whitespace_table","rows":{"mode":"keywords","include":["{{VM}}"],"exclude":[],"include_mode":"all","case_sensitive":true},"columns":[{"key":"PID","selector":{"by":"index","index":2},"value_mode":"integer"}],"value_key":"PID","cardinality":"first","source":"stdout"}}。
$UNSAFE$,
        $SAFE$
- 行选择：按关键字时使用 rows={{"mode":"keywords","include":[...],"exclude":[...],"include_mode":"all|any","case_sensitive":true}}；按行号时使用 rows={{"mode":"indices","basis":"data","indices":[...],"ranges":[...]}}；所有行使用 rows={{"mode":"all"}}。
- 列选择：整行不配置 columns；空白分列使用 parser="whitespace_table"；单字符分隔使用 parser="delimited_table" 与 delimiter。每一列使用 {{"key":"稳定大写名","selector":{{"by":"index","index":N}}|{{"by":"header","name":"列名","aliases":[...]}},"value_mode":"string|integer|number|boolean"}}；需要标量值时 value_key 必须等于其中一个 key。
- 示例：{{"name":"PID","type":"integer","extract":{{"type":"text","parser":"whitespace_table","rows":{{"mode":"keywords","include":["{{{{VM}}}}"],"exclude":[],"include_mode":"all","case_sensitive":true}},"columns":[{{"key":"PID","selector":{{"by":"index","index":2}},"value_mode":"integer"}}],"value_key":"PID","cardinality":"first","source":"stdout"}}}}。
$SAFE$
    ),
    version = '1.5',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template LIKE '%- 行选择：按关键字时使用 rows={"mode":"keywords"%';

DO $verify$
DECLARE
    extract_prompt text;
BEGIN
    SELECT content_template INTO extract_prompt
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2';

    IF extract_prompt IS NULL THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF extract_prompt LIKE '%rows={"mode":"keywords"%'
       OR extract_prompt LIKE '%每一列使用 {"key"%'
       OR extract_prompt LIKE '%- 示例：{"name":"PID"%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 包含未转义的 JSON 花括号，会被 StrictPromptLoader 误判为占位符';
    END IF;
END
$verify$;
