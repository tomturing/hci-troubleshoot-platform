-- database/seeds/03_skill_definitions.sql — 技能定义初始种子数据
-- 用途：初始化 skill_definition 表，预置平台内置的通用 Skill 定义

INSERT INTO skill_definition (
    skill_name,
    display_name,
    description,
    parameters_schema,
    output_schema,
    is_active,
    version
) VALUES (
    'disk_vendor_lifetime',
    '硬盘厂商识别与寿命判定',
    '通过解析 SATA/SAS 硬盘 SMART 信息，智能识别厂商（铠侠/东芝、英特尔、三星、美光、创见、海康、大唐存储、华为、江波龙、Foresee、建兴等）并依据各自的寿命阈值标准计算出硬盘是否需要返修。',
    '{
        "type": "object",
        "properties": {
            "smart_info": {
                "type": "string",
                "description": "硬盘 SMART 原始回显文本（如 smartctl -a /dev/sdX 的输出）"
            }
        },
        "required": ["smart_info"]
    }'::jsonb,
    '{
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["正常", "返修"],
                "description": "寿命判定结果：正常 / 返修"
            }
        },
        "required": ["status"]
    }'::jsonb,
    true,
    '1.0'
)
ON CONFLICT (skill_name) 
DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    output_schema = EXCLUDED.output_schema,
    is_active = EXCLUDED.is_active,
    version = EXCLUDED.version,
    updated_at = CURRENT_TIMESTAMP;
