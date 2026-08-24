-- 为已有环境补齐 qfk_var 工具定义；共享代码契约才是可执行能力的唯一事实源。
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_var',
    '后端信号-变量处理器',
    'qfk',
    '对变量池中显式 requires 的输入执行固定四层流水线：确定性变量提取、变量命名和归一化、类型和基数校验、受控 AI 兜底；assert 只判断，derive 原子产出一个新变量。',
    'qfk_var {{operation}}',
    '{"type":"object","description":"参数以 shared ACQUIRER_ARGS_SCHEMA 为唯一可执行契约；qfk_var 不生成命令"}'::jsonb,
    '[{"mode":"derive","operation":"feature_extract","input":"{{DESCRIPTION}}","target_variable":"vm_name","value_type":"string","cardinality":"exactly_one"}]'::jsonb,
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = CURRENT_TIMESTAMP;
