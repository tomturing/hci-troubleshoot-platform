-- ============================================================================
-- 046_update_react_output_constraint_known_vars_authority.sql
-- 描述：为统一输出约束 s1_react_output_constraint_v1 追加【已知变量权威】规则，
--       禁止在 user_questions 中重复索取已提交变量或凭空索要 SOP 未声明字段。
-- 问题：工单 Q2026101511258「硬盘坏道」——用户已提交 disk_dev 后，S2 假设生成阶段
--       ReasoningOutput.user_questions 仍续写出「请提交 disk_dev」并凭空索要未声明的
--       disk_sn/disk_type。现有反幻觉硬拦截只覆盖「结论语境+无溯源数值」，对「提问语境
--       +无依据需求项」无拦截；本迁移从 Prompt 侧加固（引擎侧另有确定性过滤兜底）。
-- 影响：所有走 SOP/ReAct 诊断的输出约束（S1~S4 统一追加）。
-- 幂等性：仅当 content_template 尚未包含【已知变量权威】时追加，可重复执行。
--       对「该行不存在」的全新/CI 环境（prompt seed 未加载）优雅跳过：新环境由已含
--       规则8的 database/seeds/02_system_prompts.sql 提供，本迁移仅用于拉齐存量 live 库。
-- 唯一调用链：migration:20261015000001
-- ============================================================================

DO $$
DECLARE
    v_rule text := E'\n'
        || '8. 【已知变量权威】系统提示中的「已知变量」列表是当前变量池的权威事实源，其优先级高于对话历史里任何早期的失败/待补原文本：
   a. **严禁**在任何面向用户的提问（含结构化输出的 user_questions）中重复索取「已知变量」中已给出值的变量；
   b. **严禁**索取本 SOP variable_schema 未声明的字段（例如已提交 disk_dev 却追问 disk_sn、disk_type）；
   c. 需要用户补充时，只能针对「本节点依赖变量」中尚未出现在「已知变量」里的**已声明**变量，且必须通过 sop_request_variable 正式弹框采集，不得在假设/提问文本里即兴索要。';
BEGIN
    RAISE NOTICE '开始执行 046 数据迁移：为 s1_react_output_constraint_v1 追加【已知变量权威】规则...';

    UPDATE system_prompt
    SET content_template = content_template || v_rule,
        version = '1.1',
        updated_at = NOW()
    WHERE name = 's1_react_output_constraint_v1'
      AND content_template NOT LIKE '%【已知变量权威】%';

    IF NOT EXISTS (
        SELECT 1 FROM system_prompt WHERE name = 's1_react_output_constraint_v1'
    ) THEN
        -- 行不存在（全新/CI 环境未加载 prompt seed）：由 seed 提供规则，本迁移优雅跳过。
        RAISE NOTICE '未找到 s1_react_output_constraint_v1（空库或未导入该 prompt），跳过追加（trace_id=migration:20261015000001）';
    ELSIF NOT EXISTS (
        -- 校验：唯一调用链标记 migration:20261015000001（行存在却缺规则 = 追加真失败，须 fail-loud）
        SELECT 1 FROM system_prompt
        WHERE name = 's1_react_output_constraint_v1'
          AND content_template LIKE '%【已知变量权威】%'
    ) THEN
        RAISE EXCEPTION 'migration:20261015000001 未生效：s1_react_output_constraint_v1 存在但未包含【已知变量权威】规则';
    ELSE
        RAISE NOTICE '✓ 成功更新系统提示词 s1_react_output_constraint_v1：追加【已知变量权威】规则';
    END IF;

    RAISE NOTICE '完成 046 数据迁移（trace_id=migration:20261015000001）';
END $$;
