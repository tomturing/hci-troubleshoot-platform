"""
QFK 后端信号模板与 LLM 提取提示词
"""

BACKEND_SIGNAL_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "BackendSignal",
    "description": "一个排查步骤对应的标准化后端信号，供 htp-agent 通过 QFK 执行",
    "type": "object",
    "required": ["namespace"],
    "additionalProperties": False,
    "properties": {
        "namespace": {
            "type": "string",
            "description": "acli 子命令空间名，决定 QFK 使用哪种 Handler 执行",
            "enum": [
                "log",
                "service",
                "vm",
                "network",
                "storage",
                "hardware",
                "platform",
                "system",
            ],
        },
        "host": {
            "type": "string",
            "description": "采集目标主机/作用域（{{HOST}} 或 cluster 表示遍历集群）",
        },
        "command": {
            "type": "string",
            "description": "（system/vm/network/storage/hardware/platform 专用）acli 子命令动作后缀",
        },
        "instruction": {
            "type": "string",
            "description": "对原始排查步骤的说明解释",
        },
        "file": {
            "type": "string",
            "description": "（log 专用）安全日志 basename（acli -f，扩展名不限，禁止目录分隔符）",
        },
        "path": {
            "type": "string",
            "description": "（log 专用）常规日志位于 /sf/log；/sf/data/local 仅可与 request_id 用于辅助关联",
        },
        "time_window": {
            "type": "string",
            "description": "（log 专用）绝对时间；相对时间须先解析为 YYYY-MM-DD[ HH[:MM:SS]]",
        },
        "source_family": {
            "type": "string",
            "enum": ["auto", "whitebox", "blackbox", "vn_blackbox", "pod"],
            "description": "（log 专用）统一日志族，通常使用 auto",
        },
        "parser": {
            "type": "string",
            "enum": [
                "plain_text", "timestamped_lines", "timestamped_blocks", "ifconfig_snapshot",
                "kv_counter_snapshot", "process_snapshot",
            ],
            "description": "（log 专用）结构 parser；通常省略并由 Catalog 选择",
        },
        "request_id": {
            "type": "string",
            "description": "（log 专用）调用链 request_id（acli -i）",
        },
        "context_lines": {
            "type": "integer",
            "minimum": 0,
            "maximum": 50,
            "description": "（log 专用）命中上下文行数（acli -c）",
        },
        "container": {
            "type": "string",
            "description": "（system 专用）容器类型: asv-con/vn-con/...",
        },
        "service": {
            "type": "string",
            "description": "（service 专用）服务名称",
        },
        "action": {
            "type": "string",
            "description": "（service 专用）动作，默认 status",
        },
        "keyword": {
            "type": "array",
            "description": "K：对比关键字列表",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "match_mode": {
            "type": "string",
            "description": "关键字组合匹配模式: or(任一) / and(全部) / not(均不出现)",
            "enum": ["or", "and", "not"],
            "default": "or",
        },
        "expected": {
            "type": "boolean",
            "description": "期望结果：true=期望出现该信号（即出现说明存在排查异常），false=期望不出现该信号（即不出现说明正常）",
            "default": True,
        },
    },
}

BACKEND_SIGNAL_PROMPT_TEMPLATE = """## 任务
你是一个 HCI 平台排障专家，需要将以下自然语言书写的"排查步骤"提炼并提取为标准格式的「后端信号」。

## 输入
<investigation_step>
{investigation_step}
</investigation_step>

## 后端信号类型说明（namespace）
- log      : 统一日志检索与判定（/sf/log 下 whitebox/blackbox/vn-blackbox/pod 均走 qfk_log；
             /sf/data/local 不是日志族，只能携带 request_id 做辅助关联搜索）
- service  : 服务运行状态检查（如 asv/redis 服务是否 running）
- vm       : 检查虚拟机（acli vm ...）
- network  : 检查网络状态（acli network ...）
- storage  : 检查虚拟存储（acli storage ...）
- hardware : 检查硬件配置状态（acli hardware ...）
- platform : 检查集群平台层（acli platform ...）
- system   : 检查 CPU/内存/磁盘指标等系统属性（acli system ...，封装 lsof/ps/lsblk/iostat/smartctl 等）

## 提取规范
1. keyword 数组只包含要匹配的比对字，**千万不能**包含具体的 shell 运行命令；
2. 对于 expected，如果步骤中表述"排查是否有报错/出现异常"，说明发现报错代表排查符合预期，expected 应设为 true；如果是"确认该服务是正常的/无报错"，则 expected 设为 false（说明检测不到关键字才说明符合健康预期）；
3. 对于 vm/network/storage 等类型，必须将 command 提取出来（如 "asan disk list"），QFK 会自动将其与 "acli storage" 拼接成完整命令；
4. 严格按照 JSON schema 输出，不要多余输出，并作为一个 JSON 数组包起来（因为一个步骤有时包含多个小排查子项）。
5. log 的 file 只能是 basename；不要生成 qfk_blackbox。省略 path 时由 Catalog 推断；若原文明确
   blackbox/vn-blackbox，可设置 source_family。时间只能使用绝对时间或 {{ABSOLUTE_TIME}}，不得传 now/-1h。
6. LOG_ifconfig.txt、LOG_ethtool_statistic.txt 等周期快照需要计数器判定时，使用
   threshold/delta/trend matcher，并提供 metric；普通报错使用 keyword/regex。

## 输出 JSON 格式：
```json
[
  {{
    "namespace": "...",
    "host": "{{HOST}}",
    "file": "mysql-managed.log",
    "path": "/sf/log/today/",
    "keyword": ["..."],
    "match_mode": "or",
    "expected": true,
    "instruction": "..."
  }}
]
```
"""
