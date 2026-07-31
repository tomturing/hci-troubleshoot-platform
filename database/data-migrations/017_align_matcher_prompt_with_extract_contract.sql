-- 修复 KBD 抽取 Prompt 与当前 v2 Schema 的残余漂移。
--
-- Matcher 和 produces 均使用唯一的声明式 Extract 契约；Matcher mode 仅 or/and/not。
-- rows.include_mode 的 all/any 是行筛选语义，不能作为 Matcher mode。
-- JSON 路径是 extract.type=json 的取值方式，而不是独立 Matcher 类型。
-- 不做 any/all 到 or/and 的兼容或候选数据改写：非法候选必须保留拒绝审计。

UPDATE system_prompt
SET content_template = regexp_replace(
    regexp_replace(
        replace(
            replace(
                replace(
                    content_template,
                    '文本产出必须使用声明式 Extract：',
                    'Matcher 与产出变量都必须使用声明式 Extract：'
                ),
                'json_path 必须有 path。',
                '需要读取 JSON 字段时必须在 match.extract 或 produces[].extract 使用 type=json 与 path；再用 state、threshold 或 exists 判定取值。'
            ),
            'json_path',
            'JSON 路径'
        ),
        $PATTERN$"match": \{\{"type": "<[^"]+>", "pattern": "<匹配式>", "mode": "[^"]+", "expected": true\}\}$PATTERN$,
        $REPLACEMENT$"match": {{"type": "<keyword|regex|state|threshold|delta|trend|exists>", "pattern": "<匹配式>", "mode": "or|and|not", "expected": true, "extract": {{"type": "text", "rows": {{"mode": "all"}}, "cardinality": "all", "source": "stdout"}}}}$REPLACEMENT$,
        'g'
    ),
    $EXAMPLE$"match": \{\{"type": "keyword", "pattern": "ClwDRDBClient", "mode": "[^"]+", "expected": true\}\}$EXAMPLE$,
    $EXAMPLE_REPLACEMENT$"match": {{"type": "keyword", "pattern": "ClwDRDBClient", "mode": "or", "expected": true, "extract": {{"type": "text", "rows": {{"mode": "all"}}, "cardinality": "all", "source": "stdout"}}}}$EXAMPLE_REPLACEMENT$,
    'g'
) || $RULE$

# 补充规则 21：Matcher/行选择模式必须严格隔离（最高优先级）
- backend 的判定模式必须在 match 中提供 extract；Matcher 与 produces 的 extract 完全复用同一份声明式 Extract Schema、运行时实现和 Admin 编辑器。
- match.mode 只允许 or、and、not；绝不可写 any 或 all。rows.include_mode 才允许 all、any，且只表达多个行筛选关键字之间的关系；绝不可写 or、and、not。
- 需要从 JSON 读取字段时，写 match.extract={{"type":"json","path":"...","cardinality":"exactly_one","source":"stdout"}} 或 produces[].extract；再以 state、threshold 或 exists 判定，不得把 JSON 路径写为 match.type。
$RULE$,
    description = description || '；Matcher/行选择模式隔离并强制 Match Extract',
    version = '1.6',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 21：Matcher/行选择模式必须严格隔离%';

DO $verify$
DECLARE
    extract_prompt text;
BEGIN
    SELECT content_template INTO extract_prompt
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2';

    IF extract_prompt IS NULL THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF extract_prompt LIKE '%"mode": "any|all"%'
       OR extract_prompt LIKE '%json_path%'
       OR extract_prompt NOT LIKE '%backend 的判定模式必须在 match 中提供 extract%'
       OR extract_prompt NOT LIKE '%绝不可写 any 或 all%'
       OR extract_prompt NOT LIKE '%"pattern": "ClwDRDBClient", "mode": "or", "expected": true, "extract"%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 未与当前 Matcher/Extract 契约对齐';
    END IF;
END
$verify$;
