-- 将 KBD 抽取从“模型侧过滤 Signal”修正为“完整 Candidate + 服务端三态门禁”。
--
-- 仅升级 Prompt；既有 KBD/revision 保持不可变。历史 Proposal 通过按批重新抽取
-- 形成新快照。服务端以 write_signal/not_exists/run_failed 保留所有门禁失败候选。

UPDATE system_prompt
SET content_template = replace(
        replace(
            replace(
                    content_template,
                    '5. Signal 只读边界：Signal 只能是只读事实采集、确定性判定或变量生产。输入中即使出现修改配置、启停/重启、删除、迁移等处置/修复动作，也不得生成 Signal；动作应留在 KBD 解决方案中，若它出现在排查描述则交由专家修正文。',
                    '5. 生成与门禁分离：有证据支持的候选都必须输出。只读事实候选通常会成为 Signal；写动作、当前 catalog 缺失命令或验证失败候选会由服务端进入 Rejected Candidate。不要因为预判门禁不通过而省略 Candidate。'
                ),
                '7. 变更动作禁止输出：若正文出现 start/stop/restart/delete/set/create/rm/kill 等写入、变更或处置语义，不生成对应 Signal，不以 require_human_confirm 或 phase=solution 方式保留；只抽取与其相邻、证据充分的只读检查事实。',
                '7. 变更动作必须作为 Candidate 输出：若正文证据明确包含 start/stop/restart/delete/set/create/rm/kill 等写入、变更或处置语义，保留原始执行语义并设置 phase=solution、needs_review=true；服务端会归入 Rejected Candidate/write_signal。禁止伪装成只读动作，也禁止直接省略。'
            ),
            '18. 诊断与处置边界：所有输出 Signal 的 phase 必须为 diagnostic；phase=solution 禁止输出。只读 list/get/status/show/check 可以生成 Signal；任何写入、配置变更、启停/重启、删除或其他修复行为都不属于 Signal。command/command_args/resource_keyword 禁止包含 |、;、&、反引号、$、重定向符或换行；不要把多条命令拼成一个 command。',
            '18. 诊断与处置标注：只读事实采集、判定和变量生产 Candidate 使用 phase=diagnostic；真实写入、配置变更、启停/重启、删除或其他修复 Candidate 使用 phase=solution。后者不会成为 Signal，但必须输出并由服务端归入 write_signal。command/command_args/resource_keyword 不拼接多条命令；保持原始执行语义，不能为了过门禁而改写。'
        ) || $RULE$

# 补充规则 25：Candidate/Signal/Rejected Candidate 三态门禁
- 只使用三个概念：Candidate 是模型识别出的全部候选；Signal 是服务端门禁通过的候选；Rejected Candidate 是门禁未通过但完整保留、交专家处理的候选。模型不得替服务端过滤 Candidate。
- qkv_task 是历史任务查询。keyword 中的启动、创建、迁移、删除只是查询条件，不是执行写动作，必须按只读 diagnostic Candidate 输出。
- 写入、配置变更、启停/重启、删除等真实执行动作仍须输出 Candidate 并标 phase=solution；服务端归入 write_signal，必须审核。
- 当前内置 aCLI catalog 是生成知识而不是模型侧门禁。优先使用已注册命令；证据明确但 catalog 缺失时仍输出 Candidate 并标 needs_review，服务端归入 not_exists，供专家确认 catalog 缺口或模型乱造。
- Schema、args、变量依赖、Matcher、编译/预运行或真实运行验证失败统一视为 run_failed。命令在 catalog 中只证明“已登记”，不证明能运行成功。
- BMC/iBMC 管理页面中的事件日志不是 HCI 平台告警。smartctl、ipmitool、dmidecode 属于 qfk_system；不得把 ipmitool mc info 或 BMC Web 页面查看伪造成 qfk_hardware。qfk_hardware 当前已注册 cpu microcode file list、gpu config get/list、hostcli hostcli。
- qfk_storage、qfk_hardware、qfk_vm、qfk_network、qfk_platform 的 command 应包含 namespace 后完整 catalog 路径，例如 command="asan disk list"，不能写 command="list" 再用 resource_keyword 补“disk”。
- match.pattern 不固化 xx、XXX、***、%(ip)s 等脱敏占位文本。keyword 不解释正则竖线；多关键字使用数组，正则选择使用 regex。exists 只判断提取结果是否存在，不读取 pattern。
$RULE$,
    description = CASE
        WHEN COALESCE(description, '') LIKE '%Candidate 三态门禁%' THEN description
        ELSE concat_ws('；', NULLIF(COALESCE(description, ''), ''), 'Candidate 三态门禁')
    END,
    version = '2.1',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 25：Candidate/Signal/Rejected Candidate 三态门禁%';

UPDATE system_prompt
SET content_template = replace(
        replace(
            content_template,
            '"orchestrate": {{"phase": "diagnostic", "action": "<可选>"',
            '"orchestrate": {{"phase": "<diagnostic|solution>", "action": "<可选>"'
        ),
        '信息不足时标记 needs_review 或减少候选；不得为了凑数量生成无下游消费者的 producer。',
        '信息不足时标记 needs_review 并忠实保留 Candidate；不得为了凑数量凭空生成无证据、无下游消费者的 producer。'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2';

UPDATE system_prompt
SET version = '2.1',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND version <> '2.1';

DO $verify$
DECLARE
    extract_prompt text;
    extract_version text;
BEGIN
    SELECT content_template, version
    INTO extract_prompt, extract_version
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2';

    IF extract_prompt IS NULL THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF extract_version <> '2.1'
       OR extract_prompt NOT LIKE '%补充规则 25：Candidate/Signal/Rejected Candidate 三态门禁%'
       OR extract_prompt NOT LIKE '%qkv_task 是历史任务查询%'
       OR extract_prompt NOT LIKE '%服务端归入 write_signal%'
       OR extract_prompt NOT LIKE '%服务端归入 not_exists%'
       OR extract_prompt NOT LIKE '%统一视为 run_failed%'
       OR extract_prompt NOT LIKE '%"signals": [%'
       OR extract_prompt LIKE '%所有输出 Signal 的 phase 必须为 diagnostic；phase=solution 禁止输出%'
       OR extract_prompt LIKE '%无法映射到 catalog 时不生成 Signal%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 未对齐 Candidate 三态门禁';
    END IF;
END
$verify$;
