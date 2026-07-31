-- 新版声明式 Extract 已成为唯一保存/运行契约；热加载 Prompt 必须同步收口。
-- 同时修正“任务详情弹窗”被外观降级为弹框截图的问题。
-- 幂等：以规则标题为哨兵；执行后用断言阻止旧 Prompt 再次进入运行环境。

UPDATE system_prompt
SET content_template = regexp_replace(
        replace(
            replace(
                replace(content_template, '"mode": "any"', '"mode": "or"'),
                'mode(any/all)',
                'mode(or/and/not)'
            ),
            'mode: any|all',
            'mode: or|and|not'
        ),
        E'[^\n]*column_mode[^\n]*(?:\n|$)',
        '',
        'g'
    ) || $RULE$

# 补充规则 20：声明式 Extract 是唯一文本取值语法（最高优先级）
- 文本输出先声明取值，再进行 Matcher 判定或写入变量。取值必须使用 type=text、rows、可选 parser/header、可选 columns[] 与 value_key；不得使用未在此契约声明的字段。
- 行选择：按关键字时使用 rows={{"mode":"keywords","include":[...],"exclude":[...],"include_mode":"all|any","case_sensitive":true}}；按行号时使用 rows={{"mode":"indices","basis":"data","indices":[...],"ranges":[...]}}；所有行使用 rows={{"mode":"all"}}。
- 列选择：整行不配置 columns；空白分列使用 parser="whitespace_table"；单字符分隔使用 parser="delimited_table" 与 delimiter。每一列使用 {{"key":"稳定大写名","selector":{{"by":"index","index":N}}|{{"by":"header","name":"列名","aliases":[...]}},"value_mode":"string|integer|number|boolean"}}；需要标量值时 value_key 必须等于其中一个 key。
- 示例：{{"name":"PID","type":"integer","extract":{{"type":"text","parser":"whitespace_table","rows":{{"mode":"keywords","include":["{{{{VM}}}}"],"exclude":[],"include_mode":"all","case_sensitive":true}},"columns":[{{"key":"PID","selector":{{"by":"index","index":2}},"value_mode":"integer"}}],"value_key":"PID","cardinality":"first","source":"stdout"}}}}。
- Matcher 的 mode 只允许 or、and、not。keyword、regex、state、threshold、delta、trend 的取值对象都可配置同一份 extract；不要因为使用 Matcher 而跳过安全转换管道。
$RULE$,
    description = description || '；声明式 Extract 与匹配模式收口至当前 Schema',
    version = '1.4',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 20：声明式 Extract 是唯一文本取值语法%';

UPDATE system_prompt
SET content_template = replace(
        replace(
            content_template,
            '配置页面上叠加错误弹框时应判为“弹框截图”，不能因为底层页面是配置页而判为“配置截图”。',
            '弹窗外观不决定截图类型：若弹窗内直接展示任务记录字段，应判为“任务截图”；只有通用提示才判为“弹框截图”。'
        ),
        '页面上出现失败 Toast、模态框或错误对话框 → 弹框，即使底层是配置页或任务页',
        '页面出现失败 Toast、模态框或错误对话框时，先判断其中是否直接展示任务或告警记录；任务详情弹窗判为任务截图，通用提示才判为弹框截图'
    ) || $RULE$

【任务详情截图优先规则】
1. 截图中的模态框只是容器，不是语义类型。若同一可见区域包含任务状态、行为/任务名，以及开始/结束时间、对象、主机等任务字段，应输出 TYPE: 任务截图。
2. 任务详情无论是列表、抽屉还是弹窗展示，均按任务截图处理；完整提取状态、行为/任务名、起始时间、结束时间、对象、主机和错误信息。
3. 只有不包含可识别任务/告警记录的通用成功、失败、警告、Toast 或确认对话框，才输出 TYPE: 弹框截图。
$RULE$,
    description = description || '；任务详情弹窗按任务截图优先分类',
    version = '1.2',
    updated_at = NOW()
WHERE name = 'kbd_vision_v1'
  AND content_template NOT LIKE '%【任务详情截图优先规则】%';

DO $verify$
DECLARE
    extract_prompt text;
BEGIN
    SELECT content_template INTO extract_prompt
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2';

    -- Schema CI 仅建立表结构、不加载业务种子，Prompt 不存在属于正常空库状态。
    -- 实际部署的 db-seed 已先写入该 Prompt；存在时必须严格验证迁移结果。
    IF extract_prompt IS NULL THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF extract_prompt LIKE '%column_mode%' OR extract_prompt LIKE '%"mode": "any"%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 仍包含已停用的 Extract/Matcher 语法';
    ELSIF extract_prompt NOT LIKE '%补充规则 20：声明式 Extract 是唯一文本取值语法%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 未写入声明式 Extract 规则';
    END IF;
END
$verify$;
