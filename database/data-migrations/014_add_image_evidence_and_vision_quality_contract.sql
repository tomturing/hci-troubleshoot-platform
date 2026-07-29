-- 将结构化截图 Evidence IR 接入信号抽取，并补齐 Vision 弹框/完整文字规则。
-- 幂等：通过 image_evidence 占位符和规则标题防重复。

UPDATE system_prompt
SET content_template = replace(
        content_template,
        E'- 解决方案：{solution}\n\n# 采集器目录',
        E'- 解决方案：{solution}\n\n# 截图 Evidence IR（JSON）\n{image_evidence}\n\n' ||
        E'只允许依据 regions[].observed_facts、text_lines、fields 生成事实型参数。' ||
        E'输入层不提供 DESCRIPTION/inferences/legacy desc；不得根据 inference 状态反推参数。' ||
        E'quality.needs_review=true 或 legacy_evidence_unavailable=true 只能生成 needs_review 候选。\n\n# 采集器目录'
    ),
    description = description || '；新增 image_evidence 结构化截图证据输入',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%{image_evidence}%';

UPDATE system_prompt
SET content_template = content_template || $RULE$

# 补充规则 13：截图证据、QFK 必填参数与可追溯来源
- 告警截图→qkv_alert，任务截图→qkv_task，弹框/Toast/模态框→qkv_dialog；不得仅凭底层页面类型替代真正承载故障语义的区域类型。
- 截图只能使用 observed_facts/text_lines/fields；输入层必须剔除 DESCRIPTION/inferences/legacy desc，禁止根据推断状态反推运行参数。
- 来自截图的信号必须在 provenance.source_refs 写入 img:N/region:...；低质量截图必须 provenance.needs_review=true。
- qfk_log 的 file 为必填安全 basename，禁止目录分隔符和控制字符，扩展名不限；无法从正文或截图可见文字确定 file 时不得生成 qfk_log。
- qfk_service 必须提供 resource_keyword；qfk_system/vm/network/storage/hardware/platform 必须提供 command。
- keyword/regex/state 必须有 pattern；threshold 必须有 value+operator；json_path 必须有 path；非法 regex 在发布前拒绝。
- `... | wc -l` 必须编译为基础列举 command + threshold.aggregation=line_count，禁止把管道写进 command。
- 自定义外部变量（如 STORAGE_PATH、DEVICE）必须在 verification_contract.variables 显式声明封闭类型；未声明或现场缺失时必须 inconclusive。
$RULE$,
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 13：截图证据、QFK 必填参数与可追溯来源%';

UPDATE system_prompt
SET content_template = replace(
        content_template,
        'qfk_log：必填 file（仅日志文件名，必须以 .log 结尾）',
        'qfk_log：必填 file（来自原文证据的安全 basename，禁止目录分隔符和控制字符，扩展名不限）'
    ) || $RULE$

# 补充规则 16：生成结构必须与保存 Schema 和运行时 Compiler 同源
- frontend（qkv_*）必须 match=null 且 produces 至少一项；backend 的 match 与 produces 严格二选一，不得同时配置。
- text extract 的 column_mode 只能是 whole/index/from_index；禁止 full_line/full/key_value/last_field 等别名或自造字段。没有可靠 path/extract 时不要生成变量。
- keyword/regex/state 必须有非空 pattern；threshold 必须有数值 value 和 operator，aggregation 只能是 first_number/line_count/duration_seconds；json_path 必须有 path。
- 只有真实写操作才设 phase=solution，且 solution 的 role 必须是 context；只读 list/get/status/show/check 即使需要人工确认仍是 diagnostic。
- qfk_log.file 的安全边界是 basename 字符集而非扩展名；允许 messages、.ini、BMC_Event_Log 和规范变量占位符，禁止路径分隔符和控制字符。
- command/resource_keyword 禁止包含管道、分号、&、反引号、$、重定向符或换行；不要把多条命令拼成一个 command。
$RULE$,
    version = '1.3',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 16：生成结构必须与保存 Schema 和运行时 Compiler 同源%';

UPDATE system_prompt
SET content_template = replace(
        replace(
            content_template,
            'qfk_log：必填 file（安全 basename，支持 .log/.txt 及其 .gz）',
            'qfk_log：必填 file（来自原文证据的安全 basename，禁止目录分隔符和控制字符，扩展名不限）'
        ),
        'qfk_log：必填 file（仅日志文件名，必须以 .log 结尾）',
        'qfk_log：必填 file（来自原文证据的安全 basename，禁止目录分隔符和控制字符，扩展名不限）'
    ) || $RULE$

# 补充规则 17：日志文件安全边界是 basename，不是扩展名
- qfk_log.file 必须逐字来自正文或截图可见证据；允许 messages、.ini、BMC_Event_Log 和规范 {{{{VAR}}}} 占位符，扩展名不限。
- file 只允许安全 basename，禁止 `/`、反斜杠、shell 控制字符、`.` 和 `..`；目录只能通过受限 path 字段表达。
$RULE$,
    version = '1.3',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 17：日志文件安全边界是 basename，不是扩展名%';

UPDATE system_prompt
SET content_template = replace(
        replace(
            content_template,
            '根据截图内容，从以下 6 种类型中选择最匹配的一个：',
            '根据真正承载故障语义的区域，从以下 7 种类型中选择最匹配的一个；配置页面叠加错误弹框时必须判为弹框截图：'
        ),
        '5. **配置截图**',
        '5. **弹框截图** — 模态框、对话框、Toast、气泡提示等承载成功/失败/警告的覆盖层' ||
        E'\n6. **配置截图**'
    ),
    description = replace(description, '配置/其他 6 种', '弹框/配置/其他 7 种'),
    updated_at = NOW()
WHERE name = 'kbd_vision_v1'
  AND content_template NOT LIKE '%**弹框截图**%';

UPDATE system_prompt
SET content_template = content_template || $RULE$

【补充质量规则】
1. 终端/日志按视觉顺序完整提取命令、参数、路径、文件名、PID、计数、表头和输出，禁止只抽 error/info 行。
2. 弹框完整提取标题、正文、错误码、对象名、按钮文字及必要页面定位信息。
3. 原始 Token（host/vm/file/path/command/threshold）禁止改写或概括。
4. DESCRIPTION 只能描述画面支持的解释；不确定推断必须写“可能/疑似”，不得伪装为可见事实。
$RULE$,
    updated_at = NOW()
WHERE name = 'kbd_vision_v1'
  AND content_template NOT LIKE '%【补充质量规则】%';

-- 单模型 Vision 即使 OCR 正确，也可能受文档上下文诱导而倒置因果。
-- 保留管理员对 Prompt 的已有定制，只追加不可绕过的认识论边界。
UPDATE system_prompt
SET content_template = content_template || $RULE$

【截图事实与因果边界】
1. 文档上下文只用于消歧截图所属对象、字段和场景，不能作为截图可见事实。
2. DESCRIPTION 只对截图可见内容做中性概括；不得补写画面中不可见的上下文内容。
3. 严禁建立截图事件与上下文其他事件之间的因果链，严禁使用“根因、根本原因、导致、引发、造成、因此、从而、可确认”等确定性归因表达。
4. 截图只能证明“画面显示了什么”，不能单独证明“为什么发生”或“它导致了什么”。
5. 无法从画面直接确认的解释应省略；不要用“可能/疑似”把上下文推测写回 DESCRIPTION。
$RULE$,
    description = description || '；事实与推断分离，禁止截图上下文因果归因',
    version = '1.1',
    updated_at = NOW()
WHERE name = 'kbd_vision_v1'
  AND content_template NOT LIKE '%【截图事实与因果边界】%';
