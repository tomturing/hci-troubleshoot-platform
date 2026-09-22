-- 重建 qfk_var 工具定义，并清理 PR #881 revert 后的存量脏数据（幽灵登记 + Prompt 旧语义段落）。
--
-- 历史背景：PR #881（9f113be3）曾以 033/034 引入旧设计 qfk_var（“四层变量处理器”：
-- assert/derive/feature_extract，纯变量处理、不生成命令），随后 74fbfc84 整体 revert 代码；
-- 数据迁移不回滚，存量环境因此遗留两类与本次设计相矛盾的脏数据：
--   1. tool_definition 中旧设计 qfk_var 登记（语义完全相反：旧=不生成命令，新=任意命令采集）；
--   2. kbd_extract_signals_v2 Prompt 尾部被 034 追加的【qfk_var 变量处理器】段落——
--      该段落向 LLM 宣告 qfk_var 可用并描述其用法，会诱导模型生成 qfk_var 信号，
--      直接违反新门禁“LLM 抽取禁止生成 qfk_var（服务端强制剥离）”。
--
-- 新设计（唯一事实源 = backend/shared/schemas/acquirer_args.py 的 ACQUIRER_ARGS_SCHEMA）：
-- qfk_var 是专家维护的通用变量采集原语（free shell 命令，经 bash_exec 执行并全量审计），
-- 默认把 stdout 全文写入产出变量，可选取值 stderr/exit_code；仅允许专家在管理端维护。
-- 全新环境由 seeds/03_qkv_qfk_tools.sql 直接插入；本迁移负责存量环境重建。
-- conversation-service 重启后会自动发布新的不可变 Tool Revision。

-- 1) 清理旧设计幽灵登记：新旧 schema 语义无交集，原地 UPDATE 无法表达“重建”，直接删除后重插。
DELETE FROM tool_definition WHERE tool_name = 'qfk_var';

-- 2) 清理 034 追加的 Prompt 段落（追加固定位于 content_template 尾部，段落起始于段落标题行），
--    并把版本号回退到种子初始值（034 曾改为 '2.6'，全新环境种子为 '1.0'）。
UPDATE system_prompt
SET content_template = regexp_replace(content_template, E'\n*【qfk_var 变量处理器】.*$', '', 's'),
    version = '1.0',
    updated_at = CURRENT_TIMESTAMP
WHERE name = 'kbd_extract_signals_v2'
  AND content_template LIKE '%【qfk_var 变量处理器】%';

-- 3) 按新设计插入 qfk_var 的 Tool Registry 可读投影（与 seeds/03_qkv_qfk_tools.sql 逐字一致）。
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_var',
    '后端信号-变量采集',
    'qfk',
    '变量采集原语：在目标主机执行专家指定的任意命令（shell/acli 均可，支持管道/重定向/{{VAR}} 输入变量），默认把命令 stdout 全文写入产出变量，可选取值 stderr/exit_code；命令经受控 bash_exec 会话执行并全量审计。仅供专家在管理端维护，LLM 抽取禁止生成（服务端强制剥离进 rejected_candidates）。',
    '{{command}}',
    '{
        "type": "object",
        "additionalProperties": false,
        "properties": {
            "command": {
                "type": "string",
                "description": "完整执行命令（可含 {{VAR}} 输入变量、shell 管道/重定向；acli 命令亦按原样执行，无需 acli 前缀约束）"
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
                "default": 60,
                "description": "采集/执行超时（秒，1-300）；QKV/QFK 通用"
            },
            "host": {
                "type": "string",
                "description": "采集目标主机/作用域（如 {{HOST}}），由运行时目标节点解析"
            },
            "stdin": {
                "type": "string",
                "description": "可选：作为命令标准输入写入的内容（可含 {{VAR}}）；缺省不发送 stdin"
            },
            "keep_on_failure": {
                "type": "boolean",
                "default": false,
                "description": "命令非零退出时仍对 stderr/exit_code 执行取值落变量（stdout 取值仍禁写）；缺省 false 时沿用全局门禁：非零退出 = 执行故障，不取值不落池"
            }
        },
        "required": ["command"]
    }'::jsonb,
    '[
        {"command": "acli system lsblk", "host": "{{HOST}}", "timeout": 60},
        {"command": "nvme list | grep -c nvme", "timeout": 30},
        {"command": "tail -50 /sf/log/sfvt_vtpdaemon.log", "timeout": 60, "stdin": null, "keep_on_failure": false}
    ]'::jsonb,
    2,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- 4) 验证：新投影必须就位、Prompt 旧段落必须清零。
DO $verify$
DECLARE
    var_tool jsonb;
    prompt_residue integer;
BEGIN
    SELECT parameters_schema INTO var_tool
    FROM tool_definition WHERE tool_name = 'qfk_var';

    IF var_tool IS NULL THEN
        RAISE EXCEPTION 'qfk_var 工具定义重建失败：记录不存在';
    END IF;
    IF NOT var_tool->'properties' ? 'command' OR var_tool->'required' IS DISTINCT FROM '["command"]'::jsonb THEN
        RAISE EXCEPTION 'qfk_var 参数投影与新设计不符：command 必填契约缺失';
    END IF;

    SELECT COUNT(*) INTO prompt_residue
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v2'
      AND content_template LIKE '%【qfk_var 变量处理器】%';

    IF prompt_residue > 0 THEN
        RAISE EXCEPTION 'kbd_extract_signals_v2 Prompt 中仍残留旧 qfk_var 段落 % 条', prompt_residue;
    END IF;
END
$verify$;
