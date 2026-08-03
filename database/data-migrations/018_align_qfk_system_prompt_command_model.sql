-- 将已部署环境的 KBD 关键信号抽取 Prompt 对齐 qfk_system 当前命令契约。
--
-- 不能修改已执行的 Atlas migration：实际 db-migrate 只扫描 data-migrations/。
-- 此处只追加高优先级规则，不改写 KBD/revision，也不猜测性转换历史 resource_keyword。

UPDATE system_prompt
SET content_template = content_template || $RULE$

# 补充规则 22：qfk_system 命令模型与资源字段隔离（最高优先级）
- qfk_system 只允许一个基础 command；所有普通命令参数只能使用 command_args 字符串数组。不得把参数拼进 command，也不得生成未在采集器契约注册的字段。
- qfk_system 禁止 resource_keyword。resource_keyword 仅属于其采集器契约明确允许的专属工具；禁止将 VM ID、镜像名或任意筛选文本作为 lsof 参数或 resource_keyword。
- lsof 的 VM/镜像行筛选必须通过 produces.extract.rows.include 的受控输出提取表达；先用 producer 从成功输出提取变量，再由下游信号用精确进程身份判定，不能把 VM ID 当作进程身份。
- qfk_system 的 producer 必须使用 match=null 且 produces 非空；producer 只负责提取变量，不配置 matcher。下游判定信号才配置 match.extract 和 matcher。
- lsof 属于高输出命令，必须显式设置 timeout；虚拟机镜像占用场景使用 timeout=120。
- 信息不足、无法构造上述合法结构时，宁可标记 needs_review 或不产出信号；绝不生成 resource_keyword 等旧字段。
$RULE$,
    description = concat_ws('；', NULLIF(COALESCE(description, ''), ''), 'qfk_system 命令模型与资源字段隔离'),
    version = '1.7',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 22：qfk_system 命令模型与资源字段隔离%';

DO $verify$
DECLARE
    extract_prompt text;
BEGIN
    SELECT content_template INTO extract_prompt
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2';

    IF extract_prompt IS NULL THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF extract_prompt NOT LIKE '%补充规则 22：qfk_system 命令模型与资源字段隔离%'
       OR extract_prompt NOT LIKE '%qfk_system 禁止 resource_keyword%'
       OR extract_prompt NOT LIKE '%command_args 字符串数组%'
       OR extract_prompt NOT LIKE '%produces.extract.rows.include%'
       OR extract_prompt NOT LIKE '%timeout=120%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 未与 qfk_system 当前命令契约对齐';
    END IF;
END
$verify$;
