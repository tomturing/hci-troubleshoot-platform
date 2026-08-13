-- 对齐 QFK Tool Registry 参数投影与共享采集契约。
--
-- 历史 seed 使用 ON CONFLICT DO NOTHING，存量环境中的 QFK 参数投影落后于
-- backend/shared/schemas/acquirer_args.py，导致已经通过 KBD 发布审查的合法信号在
-- 离线资源同步时被旧 Tool Revision 误判为 additionalProperties。这里只补齐共享
-- Schema 已支持的参数，不删除存量安全约束，也不修改 KBD 或任何离线业务资源；
-- conversation-service 重启后会自动发布新的不可变 Tool Revision。

WITH patches(tool_name, properties_patch, required_patch) AS (
    VALUES
        (
            'qfk_log',
            '{
                "timeout": {"type":"integer","minimum":1,"maximum":300,"default":60,"description":"采集/执行超时（秒，1-300）"},
                "host": {"type":"string","description":"采集目标主机/作用域（如 {{HOST}}），由运行时目标节点解析"},
                "instruction": {"type":"string","description":"信号语义说明（不直接拼入执行命令）"}
            }'::jsonb,
            NULL::jsonb
        ),
        (
            'qfk_service',
            '{
                "service": {"type":"string","description":"服务名（acli service <container> <service> status）"},
                "action": {"type":"string","enum":["status","start","stop","restart"],"default":"status","description":"服务动作；KBD 只读门禁禁止写操作"}
            }'::jsonb,
            '[]'::jsonb
        ),
        (
            'qfk_system',
            '{
                "timeout": {"type":"integer","minimum":1,"maximum":300,"default":60},
                "instruction": {"type":"string","description":"信号语义说明（不直接拼入执行命令）"},
                "command_args": {"type":"array","items":{"type":"string"},"description":"结构化命令参数"},
                "cluster": {"type":"boolean","default":false,"description":"是否在集群所有节点执行"},
                "formatter": {"type":"string","enum":["xml","csv","keyvalue","json"]},
                "container": {"type":"string","enum":["asv-con","vn-con","vn-agent","vs-cp-manager"]}
            }'::jsonb,
            NULL::jsonb
        ),
        (
            'qfk_vm',
            '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb,
            NULL::jsonb
        ),
        (
            'qfk_network',
            '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb,
            NULL::jsonb
        ),
        (
            'qfk_storage',
            '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb,
            NULL::jsonb
        ),
        (
            'qfk_hardware',
            '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb,
            NULL::jsonb
        ),
        (
            'qfk_platform',
            '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb,
            NULL::jsonb
        )
)
UPDATE tool_definition AS tool
SET parameters_schema = CASE
        WHEN patch.required_patch IS NULL THEN
            jsonb_set(
                COALESCE(tool.parameters_schema, '{}'::jsonb),
                '{properties}',
                COALESCE(tool.parameters_schema->'properties', '{}'::jsonb) || patch.properties_patch,
                true
            )
        ELSE
            jsonb_set(
                jsonb_set(
                    COALESCE(tool.parameters_schema, '{}'::jsonb),
                    '{properties}',
                    COALESCE(tool.parameters_schema->'properties', '{}'::jsonb) || patch.properties_patch,
                    true
                ),
                '{required}',
                patch.required_patch,
                true
            )
    END,
    updated_at = CURRENT_TIMESTAMP
FROM patches AS patch
WHERE tool.tool_name = patch.tool_name
  AND (
      NOT COALESCE(tool.parameters_schema->'properties', '{}'::jsonb) @> patch.properties_patch
      OR (patch.required_patch IS NOT NULL AND tool.parameters_schema->'required' IS DISTINCT FROM patch.required_patch)
  );

DO $verify$
DECLARE
    missing_count integer;
BEGIN
    WITH expected(tool_name, property_names) AS (
        VALUES
            ('qfk_log', ARRAY['timeout','host','instruction']),
            ('qfk_service', ARRAY['service','action']),
            ('qfk_system', ARRAY['timeout','instruction','command_args','cluster','formatter','container']),
            ('qfk_vm', ARRAY['timeout','instruction','command_args','resource_keyword']),
            ('qfk_network', ARRAY['timeout','instruction','command_args','resource_keyword']),
            ('qfk_storage', ARRAY['timeout','instruction','command_args','resource_keyword']),
            ('qfk_hardware', ARRAY['timeout','instruction','command_args','resource_keyword']),
            ('qfk_platform', ARRAY['timeout','instruction','command_args','resource_keyword'])
    )
    SELECT COUNT(*) INTO missing_count
    FROM expected
    JOIN tool_definition AS tool USING (tool_name)
    WHERE NOT COALESCE(tool.parameters_schema->'properties', '{}'::jsonb) ?& expected.property_names;

    IF missing_count > 0 THEN
        RAISE EXCEPTION 'QFK Tool Contract 参数投影补齐失败：% 条', missing_count;
    END IF;
END
$verify$;
