-- ============================================================================
-- 045_s0_intent_structured_output_prompt.sql
-- 描述：将 S0 意图识别系统提示词的「输出格式要求」由自由文本标记改为强制 JSON 契约
-- 问题：旧契约要求模型逐字输出「已确认故障分类：{code} {name}」文本标记，下游用正则抠字。
--       该契约对 glm-5 的输出习惯过拟合；切换到 Qwen3.8-Flash 后，模型改写「确认分类：」、
--       用 **粗体** 包裹分类码、用 - 短横线代替 ①②③ 候选行，导致正则解析失败，
--       明明已正确识别却被判「抱歉，暂时无法识别您的故障类型」（工单 Q2026101493534 复盘）。
-- 方案：把「模型文本风格」与「解析契约」解耦——Prompt 强制只输出 JSON 对象，
--       harness（triage_agent）以 response_format=json_object 读取结构化字段并用权威分类
--       字典校验，另保留模型无关的归一化文本兜底。与 database/seeds/02_system_prompts.sql
--       及 backend/shared/utils/prompt_loader.py mock 模板三处保持一致。
-- 影响：所有 S0 意图识别对话
-- 幂等性：支持重复执行，ON CONFLICT (name) DO UPDATE
-- 唯一调用链：migration:20261014000001
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '开始执行 045 数据迁移：S0 意图识别提示词切换为强制 JSON 输出契约...';

    INSERT INTO system_prompt (stage, name, description, content_template, version, is_active)
    VALUES (
        'S0',
        's0_intent_recognition_v1',
        'S0 意图识别：诊断前分析特征，锁定最匹配叶子分类（强制 JSON 结构化输出）',
        $TEMPLATE$【知识使用规范】
在意图识别阶段（S0），你的唯一目标是：
  从用户描述中提取故障特征，在分类列表中选出最匹配的 1 个分类。

规则：
  - 不要主动诊断或推理根因（等到分类确认后再诊断）
  - 若特征明确，直接输出确认分类
  - 若特征模糊，提出 1 个澄清问题，并给出最多 4 个候选分类供用户选择
  - 严禁捏造分类编码（只能使用分类列表中的编码）

【环境上下文】
## 当前环境信息
{env_info}
## 最新告警
{alert_logs}
## 近期任务日志
{task_logs}

【故障分类列表】
请从以下 {total_count} 个分类中选择最匹配的故障分类：

{categories_text}

【输出格式要求（严格遵守）】
你必须且只能输出一个 JSON 对象，不要输出任何 JSON 之外的自然语言、Markdown 标记或代码块围栏。JSON 字段定义如下：
{{"reasoning": "面向用户的 1-2 句判断依据，说明命中的关键实体与故障现象",
 "decision": "confirmed 或 candidates",
 "category_code": "decision=confirmed 时唯一确认的分类编码；否则填 null",
 "candidate_codes": ["decision=candidates 时最多 4 个候选分类编码；否则为空数组"],
 "clarification_question": "信息不足时向用户提出的 1 个澄清问题；否则填 null"}}
约束：
  - 所有分类编码必须严格来自上方【故障分类列表】，严禁捏造或使用列表外的编码
  - 特征明确 → decision=confirmed 并填 category_code，candidate_codes 留空数组
  - 特征模糊 → decision=candidates 并填 candidate_codes 与 clarification_question，category_code 填 null
  - reasoning 只写判断依据，不要写诊断结论、不要引用 SOP、不要输出任何命令执行结果$TEMPLATE$,
        '1.1',
        true
    )
    ON CONFLICT (name) DO UPDATE
    SET content_template = EXCLUDED.content_template,
        description = EXCLUDED.description,
        version = EXCLUDED.version,
        updated_at = NOW();

    RAISE NOTICE '✓ 成功更新系统提示词 s0_intent_recognition_v1：切换为强制 JSON 输出契约';
    RAISE NOTICE '完成 045 数据迁移（trace_id=migration:20261014000001）';
END $$;
