-- ============================================================================
-- 039_update_sop_react_prompt_with_prerequisites.sql
-- 描述：更新 SOP ReAct 系统提示词，添加【SOP 执行规范】章节，强调必须先执行前置条件
-- 问题：Agent 跳过 SOP 前置条件直接调用 sop_request_variable，导致 Skill 因缺少上下文数据而失败
-- 影响：所有使用 SOP 的对话，特别是需要前置条件（如获取告警、解析 node_ip）的场景
-- 幂等性：支持重复执行，ON CONFLICT (name) DO UPDATE
-- 唯一调用链：migration:20260928000002
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '开始执行 039 数据迁移：更新 SOP ReAct 系统提示词，添加前置条件执行规范...';

    INSERT INTO system_prompt (stage, name, description, content_template, version, is_active)
    VALUES (
        'S1',
        's1_sop_react_new_v1',
        'SOP ReAct 模式系统提示词：强调必须先执行前置条件，再获取变量值',
        $TEMPLATE$【SOP 排障流程导航模式】
当前执行 SOP：《{sop_title}》

【根节点：{root_node_title}】
类型：{root_node_type}
内容摘要：
{root_node_content}

【可选分支】
{root_node_branches}

{known_variables}

【SOP 执行规范 - 必须严格遵守】
1. **前置条件优先**：进入 SOP 节点后，必须先检查并执行 prerequisites（前置条件）中列出的所有步骤
2. **前置条件通常包括**：
   - 获取相关告警数据（如 `acli alert get -k "关键字"`）
   - 解析告警获取关键信息（如调用 `skill:hci-alert-parsing` 获取 node_ip、alert_type）
   - 在目标节点上执行诊断命令（如果 node_ip 不是本机，需要使用 ssh）
3. **变量获取顺序**：只有在完成前置条件、context_variables 已填充必要数据后，才能调用 sop_request_variable 获取变量值
4. **禁止跳过前置条件**：不得直接调用 sop_request_variable 而跳过前置步骤，否则 Skill 将因缺少必要上下文数据而失败

【工具使用指引】
1. 使用 get_sop_node(node_id) 获取节点的详细内容和子节点列表
2. 根据节点判断结果，使用 sop_advance(target_node_id, reasoning) 推进到子节点
3. 可同时使用诊断工具（acli、SCP 工具）收集证据
4. 到达 solution 节点时，总结解决方案并完成排障

【注意事项】
- 每次推进前请先获取节点内容，确保理解判断条件
- 在 reasoning 中解释为何选择此分支（记录推理路径）
- 可自由使用诊断工具辅助判断，工具调用和 SOP 导航可交替进行
- **严格执行前置条件**：如果节点有 prerequisites，必须先执行这些步骤，再获取变量或推进流程$TEMPLATE$,
        '1.1',
        true
    )
    ON CONFLICT (name) DO UPDATE
    SET content_template = EXCLUDED.content_template,
        description = EXCLUDED.description,
        version = EXCLUDED.version,
        updated_at = NOW();

    RAISE NOTICE '✓ 成功更新系统提示词 s1_sop_react_new_v1：添加【SOP 执行规范】章节';
    RAISE NOTICE '完成 039 数据迁移（trace_id=migration:20260928000002）';
END $$;
