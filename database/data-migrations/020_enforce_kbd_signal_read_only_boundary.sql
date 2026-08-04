-- 收口 KBD Signal 与处置动作的领域边界。
--
-- 只更新 Prompt，不改写 KBD 正文、signals_json 或 revision；已有问题案例通过
-- 专家修正文后重新抽取形成新 Proposal。处置候选由服务端 rejected_candidates
-- 审计，不再以 phase=solution Signal 保存。

UPDATE system_prompt
SET content_template = replace(
        replace(
            replace(
                content_template,
                '5. 安全默认（写操作）：凡涉及写/变更操作（acquire.args.command 命中写操作词表），必须 review.require_human_confirm=true 且 orchestrate.phase=solution，绝不自动执行；且只在排查步骤明确描述「处置/修复动作」时才抽取此类信号。',
                '5. Signal 只读边界：Signal 只能是只读事实采集、确定性判定或变量生产。输入中即使出现修改配置、启停/重启、删除、迁移等处置/修复动作，也不得生成 Signal；动作应留在 KBD 解决方案中，若它出现在排查描述则交由专家修正文。'
            ),
            '7. 写操作安全：若 acquire.tool 为 qfk_* 且 acquire.args.command 命中写/变更动词（start/stop/restart/delete/set/create/...），必须 review.require_human_confirm=true、orchestrate.phase=solution；且只在排查步骤明确描述「处置/修复动作」时才抽取此类信号，纯诊断步骤不要编造写操作。',
            '7. 变更动作禁止输出：若正文出现 start/stop/restart/delete/set/create/rm/kill 等写入、变更或处置语义，不生成对应 Signal，不以 require_human_confirm 或 phase=solution 方式保留；只抽取与其相邻、证据充分的只读检查事实。'
        ),
        '18. 诊断与处置边界：只有真实写操作才设 phase=solution，且 solution 的 role 必须是 context；只读 list/get/status/show/check 即使需要人工确认仍是 diagnostic。command/command_args/resource_keyword 禁止包含 |、;、&、反引号、$、重定向符或换行；不要把多条命令拼成一个 command。',
        '18. 诊断与处置边界：所有输出 Signal 的 phase 必须为 diagnostic；phase=solution 禁止输出。只读 list/get/status/show/check 可以生成 Signal；任何写入、配置变更、启停/重启、删除或其他修复行为都不属于 Signal。command/command_args/resource_keyword 禁止包含 |、;、&、反引号、$、重定向符或换行；不要把多条命令拼成一个 command。'
    ),
    description = CASE
        WHEN COALESCE(description, '') LIKE '%KBD Signal 只读边界%' THEN description
        ELSE concat_ws('；', NULLIF(COALESCE(description, ''), ''), 'KBD Signal 只读边界')
    END,
    version = '1.9',
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2';

UPDATE system_prompt
SET content_template = replace(
        replace(
            content_template,
            '"phase": "<diagnostic|solution>"',
            '"phase": "diagnostic"'
        ),
        '可选 command（status/restart 等）/timeout/instruction',
        'Signal 的 command 仅允许 status 等只读检查命令'
    ),
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2';

UPDATE system_prompt
SET content_template = content_template || $RULE$

# 补充规则 24：KBD Signal 只读边界（最高优先级）
- Signal 只能是只读事实采集、确定性判定或变量生产，所有输出 Signal 的 orchestrate.phase 必须为 diagnostic。
- 标题、问题描述、告警信息或有效排查步骤中出现写入、配置变更、启停/重启、删除、迁移等处置动作时，不生成 Signal，不以 phase=solution 或 require_human_confirm 形式保留。
- 处置动作应留在 KBD 解决方案中；若它误写在排查描述中，由专家修正源内容后重新抽取。不得自动改写案例正文，也不得根据单个案例硬编码抽取结果。
$RULE$,
    updated_at = NOW()
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%补充规则 24：KBD Signal 只读边界%';

DO $verify$
DECLARE
    extract_prompt text;
BEGIN
    SELECT content_template INTO extract_prompt
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2';

    IF extract_prompt IS NULL THEN
        RAISE NOTICE '未加载 kbd_extract_signals_v2 Prompt，跳过空库契约断言';
    ELSIF extract_prompt NOT LIKE '%补充规则 24：KBD Signal 只读边界%'
       OR extract_prompt NOT LIKE '%所有输出 Signal 的 orchestrate.phase 必须为 diagnostic%'
       OR extract_prompt NOT LIKE '%不以 phase=solution 或 require_human_confirm 形式保留%'
       OR extract_prompt NOT LIKE '%由专家修正源内容后重新抽取%'
       OR extract_prompt LIKE '%"phase": "<diagnostic|solution>"%'
       OR extract_prompt LIKE '%只在排查步骤明确描述「处置/修复动作」时才抽取此类信号%' THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 未对齐 KBD Signal 只读边界';
    END IF;
END
$verify$;
