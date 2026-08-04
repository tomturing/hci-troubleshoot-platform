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
- qkv_task 是历史任务查询。keyword 中的启动、创建、迁移、删除只是查询条件，不是执行写动作，必须按只读 diagnostic Candidate 输出。title/problem_description/alert_info 明确给出 HCI 平台告警名称或告警原文时，每个不同告警至少输出一个 qkv_alert Candidate；不得因已有后台检查而省略，BMC/iBMC 外部事件日志除外。
- 写入、配置变更、启停/重启、删除等真实执行动作仍须输出 Candidate 并标 phase=solution；服务端归入 write_signal，必须审核。phase 描述 Candidate 自身执行的命令，不描述它发生在修复前还是修复后；“重启后执行 lspci/lsblk 等只读验证”仍须标 diagnostic。
- 当前内置 aCLI catalog 是生成知识而不是模型侧门禁。优先使用已注册命令；证据明确但 catalog 缺失时仍输出 Candidate 并标 needs_review，服务端归入 not_exists，供专家确认 catalog 缺口或模型乱造。
- Schema、args、变量依赖、Matcher、编译/预运行或真实运行验证失败统一视为 run_failed。命令在 catalog 中只证明“已登记”，不证明能运行成功。
- BMC/iBMC 管理页面中的事件日志不是 HCI 平台告警。smartctl、ipmitool、dmidecode 属于 qfk_system；不得把 ipmitool mc info 或 BMC Web 页面查看伪造成 qfk_hardware。smartctl 必须提供能够实际运行的 command_args（例如 --scan，或采集选项加设备路径），禁止输出无参数的裸 smartctl。ipmitool mc info 只查看 BMC/MC 信息，不能用来采集 RAID 卡或适配器固件。qfk_hardware 当前已注册 cpu microcode file list、gpu config get/list、hostcli hostcli。qfk_log 的 evidence 必须逐字包含日志文件/路径或真实日志形态文本，不能把硬件/BMC 页面中的普通版本字段伪造成本机 messages 日志。
- qfk_storage、qfk_hardware、qfk_vm、qfk_network、qfk_platform 的 command 应包含 namespace 后完整 catalog 路径，例如 command="asan disk list"，不能写 command="list" 再用 resource_keyword 补“disk”。
- match.pattern 遇到 xx、XXX、***、%(ip)s 等脱敏占位文本时，不得伪装成现场可执行字面量，也不得降级改写成 address、ip、error 等更宽泛关键词来绕过门禁；仍按原证据输出 Candidate、保留脱敏 pattern 并标 needs_review，由服务端归入 run_failed 交专家补现场值。keyword pattern 数组中的每一项都必须能从逐字 evidence 或合法变量追溯，禁止在有证据项旁混入模型猜测项；regex pattern 必须能实际命中逐字 evidence，不能只表达意图却无法匹配自己的证据。keyword 不解释正则竖线；多关键字使用数组，正则选择使用 regex。exists 只判断提取结果是否存在，不读取 pattern。
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
SET content_template = replace(
        content_template,
        '由服务端归入 run_failed 交专家补现场值。keyword 不解释正则竖线；多关键字使用数组，正则选择使用 regex。',
        '由服务端归入 run_failed 交专家补现场值。keyword pattern 数组中的每一项都必须能从逐字 evidence 或合法变量追溯，禁止在有证据项旁混入模型猜测项。keyword 不解释正则竖线；多关键字使用数组，正则选择使用 regex。'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2';

UPDATE system_prompt
SET content_template = replace(
        content_template,
        'match.pattern 不固化 xx、XXX、***、%(ip)s 等脱敏占位文本。keyword 不解释正则竖线；多关键字使用数组，正则选择使用 regex。exists 只判断提取结果是否存在，不读取 pattern。',
        'match.pattern 遇到 xx、XXX、***、%(ip)s 等脱敏占位文本时，不得伪装成现场可执行字面量，也不得降级改写成 address、ip、error 等更宽泛关键词来绕过门禁；仍按原证据输出 Candidate、保留脱敏 pattern 并标 needs_review，由服务端归入 run_failed 交专家补现场值。keyword 不解释正则竖线；多关键字使用数组，正则选择使用 regex。exists 只判断提取结果是否存在，不读取 pattern。'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2';

-- 存量环境会按 015~020 逐次追加规则。仅追加规则 25 仍会留下 PR #668 的
-- “不生成 Signal/不产出信号/宁缺毋滥”等反向指令，导致版本号虽为 2.1，模型却
-- 继续静默丢弃 Candidate。这里把所有已知冲突语句收敛到三态语义；不能依赖
-- “后出现的规则优先”让模型自行解决矛盾。
UPDATE system_prompt
SET content_template = replace(
        replace(
            replace(
                replace(
                    replace(
                        replace(
                            replace(
                                replace(
                                    content_template,
                                    '从 KBD 案例的自然语言章节中，按「字段级分别抽取」第一性原理，产出关键信号集合（v2 嵌套结构）。' || E'\n' || '关键信号分两类角色：',
                                    '从 KBD 案例的自然语言章节中，按「字段级分别抽取」第一性原理，完整产出关键信号 Candidate 集合（v2 嵌套结构）。' || E'\n' || '必须区分且只使用三个概念：Candidate 是你识别出的全部候选；Signal 是服务端门禁通过的候选；Rejected Candidate 是服务端门禁未通过但完整保留、交专家处理的候选。你只负责提出 Candidate，不得在生成阶段替服务端过滤或删除候选。' || E'\n' || 'Candidate 分两类角色：'
                                ),
                                '# 采集器目录（封闭词表，acquire.tool 必须取自此处）',
                                '# 采集器与当前 aCLI catalog 知识（用于正确映射和减少乱造，不是模型侧过滤器）'
                            ),
                            '无法可靠映射为合法采集器的步骤，宁缺毋滥，不要硬造信号。',
                            '不得凭空添加正文没有的命令；但正文已构成候选而映射不确定时仍要输出 Candidate，并标 needs_review，供工程门禁与专家复核。'
                        ),
                        '复杂 awk、sed、sort、聚合、正则歧义或未知管道不得猜测；保留 evidence，标 provenance.needs_review=true。',
                        '复杂 awk、sed、sort、聚合、正则歧义或未知管道不得猜测或改写执行语义；仍保留 Candidate 与 evidence，标 provenance.needs_review=true，由服务端验证。'
                    ),
                    '信息不足、无法构造上述合法结构时，宁可标记 needs_review 或不产出信号；绝不生成 resource_keyword 等旧字段。',
                    '信息不足、无法构造合法结构时仍保留最接近原意的 Candidate 与 evidence，并标 needs_review；不得用 resource_keyword 等旧字段伪装成合法 Signal，由服务端归入 run_failed。'
                ),
                '当标题、问题描述、任务详情或任务截图明确表达启动、创建、迁移虚拟机失败，且后续检查需要故障 HOST 或 VM 时，必须先生成 qkv_task producer。keyword 使用正文或截图中稳定的任务动作，is_failed=true，produces 至少声明 HOST 和 VM；后续 QFK 通过 requires 使用这些变量。能够从失败任务取得的 HOST/VM 不得降级为未声明外部变量。',
                '当标题、问题描述、任务详情或任务截图明确表达启动、创建、迁移、删除虚拟机失败时，必须先生成 qkv_task producer。qkv_task 是查询历史任务的只读采集；keyword 中的“启动/创建/迁移/删除”只是查询条件，绝不等于执行写动作。keyword 使用正文或截图中稳定的任务动作，is_failed=true，produces 至少声明 HOST 和 VM；后续 QFK 通过 requires 使用这些变量。'
            ),
            '# 补充规则 24：KBD Signal 只读边界（最高优先级）' || E'\n' || '- Signal 只能是只读事实采集、确定性判定或变量生产，所有输出 Signal 的 orchestrate.phase 必须为 diagnostic。' || E'\n' || '- 标题、问题描述、告警信息或有效排查步骤中出现写入、配置变更、启停/重启、删除、迁移等处置动作时，不生成 Signal，不以 phase=solution 或 require_human_confirm 形式保留。' || E'\n' || '- 处置动作应留在 KBD 解决方案中；若它误写在排查描述中，由专家修正源内容后重新抽取。不得自动改写案例正文，也不得根据单个案例硬编码抽取结果。',
            '# 补充规则 24：KBD Candidate 执行语义分流边界（最高优先级）' || E'\n' || '- 只读事实采集、确定性判定或变量生产 Candidate 使用 orchestrate.phase=diagnostic，通过门禁后才成为 Signal。' || E'\n' || '- 真实写入、配置变更、启停/重启、删除、迁移等执行动作仍须完整输出 Candidate，使用 phase=solution，由服务端归入 write_signal 并强制专家审核。' || E'\n' || '- qkv_task 只查询历史任务；keyword 中出现启动、创建、迁移或删除不代表执行这些动作，必须作为 diagnostic Candidate 输出。不得自动改写案例正文，也不得根据单个案例硬编码抽取结果。'
        ),
        '无法从正文或截图可见文字确定 file 时不得生成 qfk_log。',
        '无法从正文或截图可见文字确定 file 时仍保留最接近原意的 Candidate 并标 needs_review，由服务端归入 run_failed。'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2';

UPDATE system_prompt
SET version = '2.1',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND version <> '2.1';

UPDATE system_prompt
SET content_template = replace(
        content_template,
        'smartctl、ipmitool、dmidecode 属于 qfk_system；不得把 ipmitool mc info 或 BMC Web 页面查看伪造成 qfk_hardware。qfk_hardware 当前已注册',
        'smartctl、ipmitool、dmidecode 属于 qfk_system；不得把 ipmitool mc info 或 BMC Web 页面查看伪造成 qfk_hardware。smartctl 必须提供能够实际运行的 command_args（例如 --scan，或采集选项加设备路径），禁止输出无参数的裸 smartctl。qfk_hardware 当前已注册'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%禁止输出无参数的裸 smartctl%';

UPDATE system_prompt
SET content_template = replace(
        content_template,
        'BMC/iBMC 管理页面中的事件日志不是 HCI 平台告警，不用 qkv_alert 获取。正文给出明确只读 ipmitool 命令时使用 qfk_system；仅描述 BMC Web 页面查看动作且无可执行命令时，保留最接近原意的 Candidate 并标 needs_review，不得编造已存在的命令。',
        'BMC/iBMC 管理页面中的事件日志不是 HCI 平台告警，不用 qkv_alert 获取。正文给出明确只读 ipmitool 命令时使用 qfk_system；仅描述 BMC Web 页面查看动作且无可执行命令时，保留最接近原意的 Candidate 并标 needs_review，不得编造已存在的命令。qfk_log 的 evidence 必须逐字包含日志文件/路径或真实日志形态文本，不能把硬件/BMC 页面中的普通版本字段伪造成本机 messages 日志。'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%不能把硬件/BMC 页面中的普通版本字段伪造成本机 messages 日志%';

UPDATE system_prompt
SET content_template = replace(
        replace(
            content_template,
            '禁止输出无参数的裸 smartctl。',
            '禁止输出无参数的裸 smartctl。ipmitool mc info 只查看 BMC/MC 信息，不能用来采集 RAID 卡或适配器固件。'
        ),
        'keyword pattern 数组中的每一项都必须能从逐字 evidence 或合法变量追溯，禁止在有证据项旁混入模型猜测项。',
        'keyword pattern 数组中的每一项都必须能从逐字 evidence 或合法变量追溯，禁止在有证据项旁混入模型猜测项；regex pattern 必须能实际命中逐字 evidence，不能只表达意图却无法匹配自己的证据。'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%ipmitool mc info 只查看 BMC/MC 信息%';

UPDATE system_prompt
SET content_template = content_template || E'\n- phase 描述 Candidate 自身执行的命令，不描述它发生在修复前还是修复后；“重启后执行 lspci/lsblk 等只读验证”仍须标 diagnostic，不能因上下文有重启而标 solution。',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%phase 描述 Candidate 自身执行的命令%';

UPDATE system_prompt
SET content_template = content_template || E'\n- title/problem_description/alert_info 明确给出 HCI 平台告警名称或告警原文时，每个不同告警至少输出一个 qkv_alert Candidate；不得因已有 smartctl/qfk_log 等后台检查而省略。BMC/iBMC 外部事件日志除外。',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%每个不同告警至少输出一个 qkv_alert Candidate%';

UPDATE system_prompt
SET content_template = content_template || E'\n- qfk_log 只能采集日志，不能把 .conf/.cfg/.ini/.json/.yaml 配置文件伪装成日志；配置读取应使用有明确安全路径的只读系统采集，无法映射时保留 Candidate 进入 run_failed。',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%不能把 .conf/.cfg/.ini/.json/.yaml 配置文件伪装成日志%';

UPDATE system_prompt
SET content_template = replace(
        content_template,
        'qfk_hardware 当前已注册 cpu microcode file list、gpu config get/list、hostcli hostcli。',
        'qfk_hardware 当前已注册 cpu microcode file list、gpu config get/list、hostcli hostcli。qfk_log 的 evidence 必须逐字包含日志文件/路径或真实日志形态文本，不能把硬件/BMC 页面中的普通版本字段伪造成本机 messages 日志。'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%不能把硬件/BMC 页面中的普通版本字段伪造成本机 messages 日志%';

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
       OR extract_prompt NOT LIKE '%完整产出关键信号 Candidate 集合%'
       OR extract_prompt NOT LIKE '%qkv_task 是查询历史任务的只读采集%'
       OR extract_prompt NOT LIKE '%不得降级改写成 address、ip、error 等更宽泛关键词%'
       OR extract_prompt NOT LIKE '%keyword pattern 数组中的每一项都必须能从逐字 evidence 或合法变量追溯%'
       OR extract_prompt NOT LIKE '%禁止输出无参数的裸 smartctl%'
       OR extract_prompt NOT LIKE '%不能把硬件/BMC 页面中的普通版本字段伪造成本机 messages 日志%'
       OR extract_prompt NOT LIKE '%ipmitool mc info 只查看 BMC/MC 信息%'
       OR extract_prompt NOT LIKE '%regex pattern 必须能实际命中逐字 evidence%'
       OR extract_prompt NOT LIKE '%phase 描述 Candidate 自身执行的命令%'
       OR extract_prompt NOT LIKE '%每个不同告警至少输出一个 qkv_alert Candidate%'
       OR extract_prompt NOT LIKE '%不能把 .conf/.cfg/.ini/.json/.yaml 配置文件伪装成日志%'
       OR extract_prompt NOT LIKE '%"signals": [%'
       OR extract_prompt LIKE '%所有输出 Signal 的 phase 必须为 diagnostic；phase=solution 禁止输出%'
       OR extract_prompt LIKE '%无法映射到 catalog 时不生成 Signal%'
       OR extract_prompt LIKE '%补充规则 24：KBD Signal 只读边界%'
       OR extract_prompt LIKE '%无法可靠映射为合法采集器的步骤，宁缺毋滥%'
       OR extract_prompt LIKE '%宁可标记 needs_review 或不产出信号%'
       OR extract_prompt LIKE '%match.pattern 不固化 xx、XXX、***、%(ip)s%'
       OR extract_prompt LIKE '%且后续检查需要故障 HOST 或 VM时%'
       OR extract_prompt LIKE '%且后续检查需要故障 HOST 或 VM 时%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 未对齐 Candidate 三态门禁';
    END IF;
END
$verify$;
