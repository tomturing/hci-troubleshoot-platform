-- 纠正 qkv_dialog 与 /sf/data/local 的运行语义投影。
-- executable semantics 仍以 shared Schema + Agent Handler 为准；这里仅同步 Admin 可读说明/示例。

UPDATE tool_definition
SET display_name = '前端信号-弹框日志定位',
    description = '无任务/告警承载的页面弹框复合取值能力：在当前主控 /sf/log/today 与 /sf/log/today/vt 检索弹框原文，过滤探针自身后提取 END、REQUEST_ID、HOST。存在对应失败任务时优先使用 qkv_task。',
    parameters_schema = $JSON$
    {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "keyword": {"type": "string", "description": "页面弹框原文或可唯一定位的稳定片段"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
        "paths": {
          "type": "array",
          "items": {"type": "string", "enum": ["/sf/log/today", "/sf/log/today/vt"]},
          "default": ["/sf/log/today", "/sf/log/today/vt"]
        },
        "context_lines": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 10},
        "instruction": {"type": "string"},
        "produces": {
          "type": "array",
          "default": [
            {"name": "END", "path": "end"},
            {"name": "REQUEST_ID", "path": "request_id"},
            {"name": "HOST", "path": "host"}
          ]
        }
      },
      "required": ["keyword"]
    }
    $JSON$::jsonb,
    examples = '[{"keyword":"编辑显卡核心失败","paths":["/sf/log/today","/sf/log/today/vt"],"context_lines":2}]'::jsonb,
    is_active = true,
    updated_at = NOW()
WHERE tool_name = 'qkv_dialog';

UPDATE tool_definition
SET description = '统一日志消费者：常规日志根只有 /sf/log；whitebox、blackbox、vn-blackbox 与 pods 统一通过 qfk_log + acli log get 获取。/sf/data/local 不是日志族，只允许携带 request_id 做辅助关联搜索。',
    parameters_schema = jsonb_set(
      jsonb_set(
        jsonb_set(
          parameters_schema,
          '{properties,source_family,enum}',
          '["auto","whitebox","blackbox","vn_blackbox","pod"]'::jsonb,
          true
        ),
        '{properties,path,description}',
        '"常规日志仅限 /sf/log；/sf/data/local 仅可与 request_id 同时使用"'::jsonb,
        true
      ),
      '{required}',
      '[]'::jsonb,
      true
    ),
    updated_at = NOW()
WHERE tool_name = 'qfk_log';

UPDATE tool_definition
SET description = '领域服务域为 asv(vt/虚拟平台)、anet(vn/虚拟网络)、asan(vs/虚拟存储)、host(宿主机/容器管理)。当前版本执行前以 aCLI capability probe 为准；观察节点只暴露 asv/anet/host。',
    updated_at = NOW()
WHERE tool_name = 'qfk_service';
