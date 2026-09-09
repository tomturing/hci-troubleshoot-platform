-- Align multi-agent Signal extraction prompts and descriptive template snapshots with
-- the executable code contract. qkv_effect remains an expert-authored capability and
-- qkv_case_context remains a deterministic semantic context input.

ALTER TABLE signal_best_practice
    ADD COLUMN IF NOT EXISTS source_revision INT,
    ADD COLUMN IF NOT EXISTS source_checksum VARCHAR(64),
    ADD COLUMN IF NOT EXISTS signal_id VARCHAR(128);

ALTER TABLE signal_best_practice
    DROP CONSTRAINT IF EXISTS uq_signal_best_practice_source_revision_signal;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_signal_best_practice_source_checksum_signal'
    ) THEN
        ALTER TABLE signal_best_practice
            ADD CONSTRAINT uq_signal_best_practice_source_checksum_signal
            UNIQUE (source_kbd_id, source_checksum, signal_id);
    END IF;
END $$;

-- 模板目录可调整或重建，但历史黄金实例必须保留；删除模板时只清空引用。
ALTER TABLE signal_best_practice
    DROP CONSTRAINT IF EXISTS signal_best_practice_template_id_fkey;
ALTER TABLE signal_best_practice
    ADD CONSTRAINT signal_best_practice_template_id_fkey
    FOREIGN KEY (template_id) REFERENCES signal_modeling_template(id) ON DELETE SET NULL;

UPDATE system_prompt
SET content_template = replace(
        replace(
            content_template,
            '受限的 13 种采集工具词表之一',
            '受限的 12 种可自动建模采集工具词表之一'
        ),
        E'- qkv_effect: 恢复后效果验证（条件型，严禁作为唯一生产者）\n',
        ''
    ),
    description = '关键信号分类 Agent：在 12 类可自动建模工具中进行属性与语义双重视角审查；效果验证由专家维护',
    version = '1.1',
    updated_at = NOW()
WHERE name = 'kbd_signal_classify_v1';

UPDATE system_prompt
SET content_template = replace(
        content_template,
        '你的任务是为已经完成分类的单个信号意图，构建完全符合 v2 Schema 契约的标准可执行 JSON。',
        '你的任务是为已经完成分类的单个信号意图，依据注入的当前工具机器契约构建完全符合 v2 Schema 的标准可执行 JSON；不得生成 qkv_effect，效果验证契约由专家在审核阶段维护。'
    ),
    version = '1.1',
    updated_at = NOW()
WHERE name = 'kbd_signal_model_v1';

UPDATE signal_modeling_template
SET acquire_schema = '{"type":"object","additionalProperties":false,"required":["host","vm_id"],"properties":{"host":{"type":"string"},"vm_id":{"type":"string"},"capture_mode":{"type":"string","enum":["baseline_then_optional_wake"],"default":"baseline_then_optional_wake"},"timeout":{"type":"integer","minimum":1,"maximum":60,"default":60},"instruction":{"type":"string"}}}'::jsonb,
    variable_protocol = '{"produces":["VM_CONSOLE_STATE","VM_CONSOLE_SUMMARY","VM_CONSOLE_CONFIDENCE","VM_CONSOLE_ARTIFACT_ID"],"requires":["HOST","VM_ID"]}'::jsonb,
    anti_patterns = ARRAY['不得并入直接生产者集合', '必须满足 HOST+VM_ID 先决条件', '禁止自由命令、路径和任意按键字段']::varchar[],
    updated_at = NOW()
WHERE tool_name = 'qkv_vm_console';

UPDATE signal_modeling_template
SET description = '条件型效果验证生产者：由专家声明修复后期望、只读观测通道和确定性判定规则',
    acquire_schema = '{"type":"object","additionalProperties":false,"required":["expectation"],"properties":{"usage":{"type":"string","enum":["remediation_verify","symptom_confirm"]},"expectation":{"type":"object"},"host":{"type":"string"},"timeout":{"type":"integer","minimum":1,"maximum":60,"default":60},"instruction":{"type":"string"}}}'::jsonb,
    variable_protocol = '{"produces":["EFFECT_STATUS","EFFECT_CHECKED_AT","EFFECT_EVIDENCE"],"requires":"由 expectation 中的运行时占位符确定"}'::jsonb,
    anti_patterns = ARRAY['不得由流水线从处置叙事自动臆造', '不得作为 KBD 的唯一生产者', 'remediation_verify 必须位于 remediation phase']::varchar[],
    updated_at = NOW()
WHERE tool_name = 'qkv_effect';
