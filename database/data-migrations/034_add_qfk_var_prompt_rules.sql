-- 将 qfk_var 的纯变量边界写入生产 Prompt，防止模型把它误生成 aCLI 命令。
UPDATE system_prompt
SET content_template = content_template || E'\n\n【qfk_var 变量处理器】qfk_var 是纯变量处理器，不生成 aCLI 命令；必须显式声明 requires。assert 只允许 compare/exists，规则写在 acquire.args，match=null 且 produces=[]；derive 要求 match=null 且 produces 恰好一个。feature_extract 固定执行“确定性变量提取→变量命名和归一化→类型和基数校验→AI 兜底”四层流水线；稳定多候选不得调用 AI，AI 值必须逐字存在于输入证据。',
    version = '2.6',
    updated_at = CURRENT_TIMESTAMP
WHERE name = 'kbd_extract_signals_v2'
  AND content_template NOT LIKE '%【qfk_var 变量处理器】%';
