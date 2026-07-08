"""
QFK 后端信号模板与 LLM 提取提示词
"""

BACKEND_SIGNAL_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "BackendSignal",
    "description": "一个排查步骤对应的标准化后端信号，供 htp-agent 通过 QFK 执行",
    "type": "object",
    "required": ["signal_type", "target", "keywords"],
    "properties": {
        "signal_type": {
            "type": "string",
            "description": "信号类型，决定 QFK 使用哪种 Function 执行",
            "enum": [
                "log_keyword",
                "service_status",
                "vm_state",
                "network_check",
                "storage_state",
                "hardware_state",
                "platform_state",
                "system_metric",
            ],
        },
        "target": {
            "type": "object",
            "description": "检查目标（Q：查什么）",
            "properties": {
                "scope": {
                    "type": "string",
                    "description": "查询范围限定，如：主节点、备节点",
                },
                "resource": {
                    "type": "string",
                    "description": "具体资源名称，如日志文件名或服务名",
                },
                "path": {
                    "type": "string",
                    "description": "（log专用）日志文件所在目录，如 /sf/log/today/",
                },
                "time_window": {
                    "type": "string",
                    "description": "限制时间范围",
                },
            },
        },
        "keywords": {
            "type": "array",
            "description": "K：对比关键字列表",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "match_mode": {
            "type": "string",
            "description": "关键字匹配模式: any(或) / all(与)",
            "enum": ["any", "all"],
            "default": "any",
        },
        "expected": {
            "type": "boolean",
            "description": "期望结果：true=期望出现该信号（即出现说明存在排查异常），false=期望不出现该信号（即不出现说明正常）",
            "default": True,
        },
        "description": {
            "type": "string",
            "description": "对原始排查步骤的说明解释",
        },
        "container": {
            "type": "string",
            "description": "（service专用）容器类型: asv/anet/host",
        },
        "sub_command": {
            "type": "string",
            "description": "（vm/network/storage等子命名空间专用）具体要拼接的 acli 子命名空间动作后缀",
        },
    },
}

BACKEND_SIGNAL_PROMPT_TEMPLATE = """## 任务
你是一个 HCI 平台排障专家，需要将以下自然语言书写的"排查步骤"提炼并提取为标准格式的「后端信号」。

## 输入
<investigation_step>
{investigation_step}
</investigation_step>

## 后端信号类型说明（signal_type）
- log_keyword    : 日志文件内容检索（如在 mysql-managed.log 中搜特定错误）
- service_status : 服务运行状态检查（如 asv/redis 服务是否 running）
- vm_state       : 检查虚拟机（acli vm ...）
- network_check  : 检查网络状态（acli network ...）
- storage_state  : 检查虚拟存储（acli storage ...）
- hardware_state : 检查硬件配置状态（acli hardware ...）
- platform_state : 检查集群平台层（acli platform ...）
- system_metric  : 检查 CPU/内存/磁盘指标等系统属性（acli system ...）

## 提取规范
1. keywords 数组只包含要匹配的比对字，**千万不能**包含具体的 shell 运行命令；
2. 对于 expected，如果步骤中表述"排查是否有报错/出现异常"，说明发现报错代表排查符合预期，expected 应设为 true；如果是"确认该服务是正常的/无报错"，则 expected 设为 false（说明检测不到关键字才说明符合健康预期）；
3. 对于 vm_state/network_check/storage_state 等类型，必须将 sub_command 提取出来（如 "asan disk list"），QFK 会自动将其与 "acli storage" 拼接成完整命令；
4. 严格按照 JSON schema 输出，不要多余输出，并作为一个 JSON 数组包起来（因为一个步骤有时包含多个小排查子项）。

## 输出 JSON 格式：
```json
[
  {{
    "signal_type": "...",
    "target": {{
      "scope": "...",
      "resource": "...",
      "path": "..."
    }},
    "keywords": ["..."],
    "match_mode": "any",
    "expected": true,
    "description": "..."
  }}
]
```
"""
