# Agent 资源（SOP / Tool / Skill）定义模版

> **受众**：编写 SOP、Tool、Skill 的开发和运维人员。
>
> **测评先行**：定义模版的同时定义测评用例。写完一个分支就在本地跑一次（`uv run python -m scripts.eval run --resource ...`），通过率达标再提交 PR。
>
> **相关文档**：
> - [Agent 能力边界与演进方向](./agent-能力边界与演进方向.md) — 各层当前不支持什么、替代方案、扩展 Roadmap
> - [Agent 测评与 GitOps 全生命周期方案](../03-测评与GitOps/agent-测评与GitOps方案.md) — 如何测评、CI 门禁如何拦截退化

---

## 场景设计

以下模版基于一个虚构但真实的运维场景：

> **场景**：生产环境某微服务响应超时，根因是数据库连接池耗尽。需通过 SOP 决策树逐步排查：确认是否为连接池问题 → 分析慢查询 → 识别资源瓶颈 → 给出修复方案。

### 资源清单与文件路径

按 GitOps 目录结构（详见 [agent-测评与GitOps方案.md](../03-测评与GitOps/agent-测评与GitOps方案.md) §二，**当前规划中，目录尚未创建**），本场景需要创建 6 个文件：

```
agent-resources/                              ← 仓库根目录
│
├── tools/                                    ← 每个 Tool 一个 .yaml
│   ├── db_pool_analyzer.yaml                 ← §1.1 模版
│   ├── service_log_collector.yaml            ← §1.2 模版
│   └── connection_tracker.yaml               ← §1.3 模版
│
├── skills/                                   ← 每个 Skill 一个 .yaml
│   ├── db-pool-saturation-detector.yaml      ← §2.1 模版
│   └── slow-query-classifier.yaml            ← §2.2 模版
│
└── sops/                                     ← 每个 SOP 一个 .md
    └── sop-db-pool-exhaustion.md             ← §3.1 模版
```

测评用例放在独立目录（不跟资源定义混在一起）：

```
tests/agent-resources/
├── tools/
│   └── db_pool_analyzer.yaml                 ← 工具测评用例
├── skills/
│   ├── db-pool-saturation-detector.yaml      ← 技能测评用例
│   └── slow-query-classifier.yaml
└── sops/
    └── sop-db-pool-exhaustion.yaml           ← SOP 测评用例（§3.3 模版）
```

### 创建顺序（依赖关系决定）

SOP 依赖 Tool 和 Skill，Tool 无依赖。按以下顺序创建：

```
1. 先创建 Tool（3 个 .yaml）                     ← SOP 的 tool_call 变量引用它们
2. 再创建 Skill（2 个 .yaml）                     ← SOP 的 skill_call 变量引用它们，Skill 的 allowed_tools 引用 Tool
3. 最后创建 SOP（1 个 .md）                        ← variable_schema 引用上述 Tool 和 Skill
4. 补充测评用例（放在 tests/agent-resources/）     ← 引用 SOP/Tool/Skill 名称
```

### 资源依赖关系

```
SOP: sops/sop-db-pool-exhaustion.md
  ├── variable_schema
  │   ├── service_name    → user_input（无需 Tool/Skill）
  │   ├── node_ip          → env_injection（无需 Tool/Skill）
  │   ├── db_pool_status   → tool_call: db_pool_analyzer ──→ tools/db_pool_analyzer.yaml
  │   ├── pool_saturation   → skill_call: db-pool-saturation-detector ──→ skills/db-pool-saturation-detector.yaml
  │   │                                                                     └── allowed_tools: db_pool_analyzer
  │   ├── slow_query_type   → skill_call: slow-query-classifier ──→ skills/slow-query-classifier.yaml
  │   │                                                             └── allowed_tools: connection_tracker, service_log_collector
  │   └── log_snapshot       → tool_call: service_log_collector ──→ tools/service_log_collector.yaml
  └── decision tree
      ├── n-1: 确认连接池状态（调用 get_sop_node）
      ├── n-1-1: 连接池饱和 → 分析慢查询（调用 connection_tracker + slow-query-classifier）
      ├── n-1-2: 连接池正常 → 排查网络/应用层
      └── n-1-3: 连接数不足 → 检查配置/扩容
```

---

## 一、Tool 模版

### 1.1 Tool: `db_pool_analyzer`

```yaml
# ============================================================
# Tool: db_pool_analyzer
# 描述: 分析目标节点上指定服务的数据库连接池状态
# ============================================================
tool_name: "db_pool_analyzer"
display_name: "数据库连接池分析器"
category: "acli"
version: "1.0"
is_active: true
risk_level: 1                         # 只读操作

# ── LLM 上下文注入 ──
description: |
  分析目标节点上指定服务的数据库连接池状态。返回连接池关键指标：
  - active_connections: 当前活跃连接数
  - idle_connections: 空闲连接数
  - max_connections: 连接池最大容量
  - waiting_requests: 等待获取连接的请求数
  - avg_wait_time_ms: 平均等待时间（毫秒）
  - connection_timeouts: 最近 5 分钟的获取连接超时次数

  使用场景：
  - 服务响应超时、错误日志中出现 "connection timeout" 或 "pool exhausted"
  - 需要确认数据库连接池是否饱和

parameters_schema:
  type: object
  properties:
    node_ip:
      type: string
      format: ipv4
      description: "目标节点 IP 地址"
    service_name:
      type: string
      description: "目标服务名称，如 api-gateway、case-service"
    port:
      type: integer
      default: 9182
      description: "数据库连接池 metrics 端点端口"
  required: [node_ip, service_name]

usage_template: "db_pool_analyzer --node {node_ip} --service {service_name}"

examples:
  - node_ip: "192.168.1.100"
    service_name: "api-gateway"
  - node_ip: "192.168.1.101"
    service_name: "case-service"
    port: 9183

# ── 输出 Schema（Trace 可解析） ──
output_schema:
  type: object
  properties:
    exit_code:        { type: integer }
    stdout:           { type: string }
    stderr:           { type: string }
    execution_time_ms: { type: integer }
    parsed:
      type: object
      properties:
        active_connections:  { type: integer }
        idle_connections:    { type: integer }
        max_connections:     { type: integer }
        waiting_requests:    { type: integer }
        avg_wait_time_ms:    { type: number }
        connection_timeouts: { type: integer }
        utilization_pct:     { type: number }

# 测评用例见 tests/agent-resources/tools/db_pool_analyzer.yaml (§1.5)
```

### 1.2 Tool: `service_log_collector`

```yaml
# ============================================================
# Tool: service_log_collector
# 描述: 按时间范围和关键字采集服务日志
# ============================================================
tool_name: "service_log_collector"
display_name: "服务日志采集器"
category: "acli"
version: "1.0"
is_active: true
risk_level: 1

description: |
  在目标节点上按时间范围和关键字过滤采集服务日志。
  支持多关键字 AND/OR 组合，支持正则表达式匹配。

  使用场景：
  - 排查特定时间段的异常日志
  - 统计连接超时、慢查询等错误模式

parameters_schema:
  type: object
  properties:
    node_ip:
      type: string
      format: ipv4
      description: "目标节点 IP"
    service_name:
      type: string
      description: "目标服务名称"
    time_range:
      type: object
      properties:
        start: { type: string, description: "开始时间 ISO8601" }
        end:   { type: string, description: "结束时间 ISO8601", default: "now" }
      required: [start]
    keywords:
      type: array
      items: { type: string }
      description: "搜索关键字列表"
    match_mode:
      type: string
      enum: [any, all]
      default: "any"
      description: "any=任意关键字匹配, all=全部关键字匹配"
    max_lines:
      type: integer
      default: 500
      description: "最大返回行数"
  required: [node_ip, service_name, time_range]

usage_template: >
  service_log_collector --node {node_ip} --service {service_name}
  --start "{time_range.start}" --keywords {keywords}

examples:
  - node_ip: "192.168.1.100"
    service_name: "api-gateway"
    time_range: { start: "2026-06-26T10:00:00Z" }
    keywords: ["connection timeout", "pool exhausted"]
    match_mode: "any"

output_schema:
  type: object
  properties:
    exit_code: { type: integer }
    stdout:    { type: string }
    stderr:    { type: string }
    parsed:
      type: object
      properties:
        total_lines:    { type: integer }
        matched_lines:  { type: integer }
        log_entries:
          type: array
          items:
            type: object
            properties:
              timestamp: { type: string }
              level:     { type: string }
              message:   { type: string }
              source:    { type: string }

# 测评用例见 tests/agent-resources/tools/service_log_collector.yaml
```

### 1.3 Tool: `connection_tracker`

```yaml
# ============================================================
# Tool: connection_tracker
# 描述: 跟踪当前活跃的数据库连接及其来源
# ============================================================
tool_name: "connection_tracker"
display_name: "数据库连接跟踪器"
category: "acli"
version: "1.0"
is_active: true
risk_level: 1

description: |
  查询 PostgreSQL 的 pg_stat_activity，返回当前活跃连接的详细信息：
  - 连接来源（客户端 IP/应用名）
  - 当前执行的查询 / 事务状态
  - 连接持续时间
  - 等待事件（如锁等待）

parameters_schema:
  type: object
  properties:
    node_ip:
      type: string
      format: ipv4
    filter_state:
      type: string
      enum: [active, idle, idle_in_transaction, all]
      default: "active"
    min_duration_sec:
      type: integer
      default: 5
      description: "只返回持续时间 >= 此值的连接"
    group_by:
      type: string
      enum: [application, client_addr, wait_event, query]
      default: "application"
      description: "按此维度分组统计"
  required: [node_ip]

usage_template: >
  connection_tracker --node {node_ip}
  --filter {filter_state} --min-duration {min_duration_sec}

examples:
  - node_ip: "192.168.1.100"
    filter_state: "active"
    min_duration_sec: 10
    group_by: "application"

output_schema:
  type: object
  properties:
    exit_code: { type: integer }
    parsed:
      type: object
      properties:
        total_connections: { type: integer }
        active_count:     { type: integer }
        idle_count:       { type: integer }
        idle_in_txn_count: { type: integer }
        waiting_count:    { type: integer }
        groups:
          type: array
          items:
            type: object
            properties:
              group_key:  { type: string }
              count:      { type: integer }
              max_duration_sec: { type: number }
              avg_duration_sec: { type: number }
        top_queries:
          type: array
          items:
            type: object
            properties:
              query_preview: { type: string }
              duration_sec:  { type: number }
              state:         { type: string }
              wait_event:    { type: string }

# 测评用例见 tests/agent-resources/tools/connection_tracker.yaml
```

### 1.4 Tool YAML 字段说明

Tool YAML 的字段就是 `POST /api/v1/tools` 的请求体，和 Web UI 表单一一对应。

| 字段 | 中文名称 | 类型 | 必填 | 作用 | 示例 |
|------|---------|------|:--:|------|------|
| `tool_name` | 工具标识 | string(100) | ✅ | 唯一标识，snake_case。LLM Function Calling 用它匹配工具。创建后不可修改 | `db_pool_analyzer` |
| `display_name` | 展示名称 | string(200) | ✅ | 管理台展示名，不注入 LLM prompt | `数据库连接池分析器` |
| `category` | 工具分类 | string(50) | ✅ | 执行路由：`acli`（命令执行）`\| sop`（导航工具）`\| scp`（平台API） | `acli` |
| `description` | 功能描述 | text | ✅ | **注入 LLM Function Calling 上下文**。描述工具功能、参数含义、使用场景、约束条件。LLM 据此决定何时调用此工具 | `分析目标节点上指定服务的数据库连接池状态...` |
| `parameters_schema` | 参数定义 | JSONB | ✅ | OpenAI Function Calling 格式的 JSON Schema。定义 `properties` + `required`。LLM 据此生成调用参数 | `{"type":"object","properties":{"node_ip":{"type":"string","format":"ipv4"}},"required":["node_ip"]}` |
| `usage_template` | 调用模板 | text | ❌ | 命令行调用模板，用于 acli 类工具的执行器解析参数 | `db_pool_analyzer --node {node_ip}` |
| `examples` | 调用示例 | JSONB | ❌ | 调用示例数组，辅助 LLM 理解工具用法 | `[{"node_ip":"192.168.1.100"}]` |
| `risk_level` | 风险等级 | int | ✅ | `1`=只读 `\| 2`=写入 `\| 3`=高危。影响前端"自动执行"策略（Safe-only 模式只自动执行 risk_level=1 的工具） | `1` |
| `is_active` | 启用状态 | bool | ✅ | `false` 时 LLM 看不到此工具，调用时校验报错 | `true` |
| `version` | 版本号 | string(20) | ✅ | 接口版本号 | `1.0` |
| `created_at` | 创建时间 | timestamptz | 自动 | 数据库自动填充 | `2026-06-29T10:00:00Z` |
| `updated_at` | 更新时间 | timestamptz | 自动 | 数据库自动更新 | `2026-06-29T12:00:00Z` |
| `output_schema` | 输出定义 | JSONB | ❌ | **规划中**。定义工具输出的 JSON Schema，用于 Trace 解析和确定性评分。当前 ORM 中尚不存在此字段 | `{"properties":{"exit_code":{"type":"integer"}}}` |

**关键理解**：`description` 和 `parameters_schema` 是 LLM 做 Function Calling 决策的核心依据——description 告诉 LLM "什么时候用这个工具"，parameters_schema 告诉 LLM "怎么传参数"。

### 1.5 Tool 测评用例示例

```yaml
# tests/agent-resources/tools/db_pool_analyzer.yaml
# 文件名与 agent-resources/tools/db_pool_analyzer.yaml 对应

eval_type: "deterministic"
stability_trials: 3          # 确定性工具 3 次足够
pass_threshold: 95           # 工具比 SOP 要求更高（纯代码逻辑，无模型不确定性）

# ── 确定性检查 ──
deterministic_checks:
  param_validation:
    - "node_ip 必须匹配 IPv4 格式或主机名格式"
    - "service_name 不为空"
    - "port 为正整数，默认 9182"

  output_validation:
    - "exit_code 必须存在（0=成功，非0=失败）"
    - "stdout 包含 parsed JSON 或原始指标文本"
    - "失败时 stderr 必须包含可读的错误原因"

  schema_validation:
    - "parsed.active_connections >= 0"
    - "parsed.max_connections > 0"
    - "parsed.utilization_pct 在 [0, 100] 范围内"
    - "parsed.waiting_requests <= parsed.max_connections"
    - "parsed.avg_wait_time_ms >= 0"

  security_check:
    - "返回内容不包含数据库密码"
    - "返回内容不包含 API Key"

# ── 正负向用例 ──
cases:
  # 正向: 正常返回
  - case_id: "db-pool-normal"
    type: "positive"
    description: "正常节点应返回完整连接池指标"
    input:
      tool_args: { node_ip: "192.168.1.100", service_name: "api-gateway" }
    expected:
      exit_code: 0
      output_has_keys: [active_connections, idle_connections, max_connections, waiting_requests, utilization_pct]
      active_connections_gt: 0    # 至少有一个活跃连接

  # 正向: 空载服务
  - case_id: "db-pool-idle"
    type: "positive"
    description: "空闲服务应返回 utilization_pct ≈ 0"
    input:
      tool_args: { node_ip: "192.168.1.100", service_name: "batch-job" }
    expected:
      exit_code: 0
      utilization_pct_lt: 10      # 利用率 < 10%
      waiting_requests: 0

  # 负向: 服务不存在
  - case_id: "db-pool-service-not-found"
    type: "negative"
    description: "不存在的服务应返回明确错误"
    input:
      tool_args: { node_ip: "192.168.1.100", service_name: "nonexistent-svc" }
    expected:
      exit_code: 1
      stderr_contains: ["service not found", "no such service", "connection refused"]
      no_crash: true

  # 负向: IP 不可达
  - case_id: "db-pool-unreachable"
    type: "negative"
    description: "不可达的 IP 应在超时后返回错误"
    input:
      tool_args: { node_ip: "10.255.255.1", service_name: "api-gateway" }
    expected:
      exit_code: 1
      stderr_contains: ["timeout", "unreachable", "no route"]
      execution_time_ms_lt: 15000   # 15s 内必须超时返回

# ── 效率基线 ──
efficiency:
  baseline_latency_ms: 3000
  baseline_tokens: 150
  max_latency_ms: 15000
  degradation_threshold: 0.5    # 延迟劣化 > 50% 扣分

# ── 健壮性 ──
robustness:
  - scenario: "service_not_running"
    expected: "exit_code=1, stderr 包含错误原因，不无限重试"
  - scenario: "port_blocked"
    expected: "exit_code=1, stderr 包含 timeout/permission denied"
  - scenario: "concurrent_calls"
    expected: "并发 3 次调用互不干扰，各自返回正确结果"
```


## 二、Skill 模版

### 2.1 Skill: `db-pool-saturation-detector`

```yaml
# ============================================================
# Skill: db-pool-saturation-detector
# 描述: 智能判断数据库连接池是否饱和，给出饱和度评分与根因推断
# ============================================================
skill_name: "db-pool-saturation-detector"
display_name: "数据库连接池饱和度检测"
version: "1.0"
is_active: true

# ── Discovery 阶段（~100 tokens） ──
description: |
  根据 db_pool_analyzer 的输出了判断数据库连接池饱和程度。
  触发条件：连接池 utilization_pct > 70% 或 waiting_requests > 0 或
  connection_timeouts > 0。

# ── Activation 阶段（< 5000 tokens） ──
instructions_md: |
  ## 概述
  根据连接池指标判断池饱和度级别，推断可能的根因方向。

  ## 步骤 1：饱和度分级
  根据以下规则判定饱和度级别：

  | utilization_pct | waiting_requests | timeouts | 级别 | 含义 |
  |----------------|-----------------|----------|------|------|
  | ≥ 95% | ≥ 10 | > 0 | critical | 连接池已耗尽，服务不可用 |
  | ≥ 80% | ≥ 5 | > 0 | high | 连接池严重饱和，即将耗尽 |
  | ≥ 70% | 1-4 | 0 | warning | 连接池压力较大，需关注 |
  | < 70% | 0 | 0 | normal | 连接池正常 |

  ## 步骤 2：根因方向推断
  - critical + avg_wait_time_ms > 5000 → 慢查询阻塞（检查 slow-query-classifier）
  - critical + idle_connections 仍较高 → 连接池配置 max_connections 不足
  - high + waiting_requests 持续增长 → 请求突发（检查上游流量）
  - warning + connection_timeouts 增长趋势 → 渐进式恶化（检查连接泄漏）

  ## 步骤 3：趋势判断（如有历史数据）
  对比 5 分钟前的指标变化趋势，判断是在恶化、改善还是维持。

  ## 踩坑提醒
  - utilization_pct 高但 waiting_requests=0 不一定有问题（可能是高效的短连接）
  - avg_wait_time_ms 的突增比绝对值更有意义
  - 不同数据库驱动的 max_connections 含义不同（有的含 idle 有的不含）

  ## 输出格式

      {
        "ok": true,
        "value": "<saturation_level>|<inferred_cause>|<urgency>",
        "confidence": "high|medium|low",
        "details": {
          "saturation_level": "critical|high|warning|normal",
          "inferred_cause": "slow_query_blocking|config_insufficient|traffic_surge|connection_leak|normal",
          "urgency": "immediate|high|medium|low",
          "supporting_evidence": ["证据1", "证据2"]
        }
      }

  若无法确定：

      {"ok": false, "error": "<原因>"}

compatibility: "HCI v2.x+，依赖 db_pool_analyzer Tool 的输出"
license: "MIT"
allowed_tools: "db_pool_analyzer"

metadata_json:
  author: "hci-team"
  category: "database"
  tags: [database, connection-pool, saturation, postgresql]

# ── 资源文件 ──
assets_json:
  - filename: "saturation_rules.json"
    type: "data"
    content: |
      {
        "levels": {
          "critical": { "utilization_min": 95, "waiting_min": 10, "timeouts_required": true },
          "high":     { "utilization_min": 80, "waiting_min": 5,  "timeouts_required": true },
          "warning":  { "utilization_min": 70, "waiting_min": 1,  "timeouts_required": false },
          "normal":   { "utilization_max": 70, "waiting_max": 0,  "timeouts_max": 0 }
        },
        "cause_patterns": {
          "slow_query_blocking":     { "indicators": ["avg_wait_time_ms > 5000", "active_connections high"] },
          "config_insufficient":     { "indicators": ["idle_connections < 5", "max_connections < 100"] },
          "traffic_surge":           { "indicators": ["waiting_requests growing", "active_connections spiking"] },
          "connection_leak":         { "indicators": ["idle_connections growing", "active_connections normal"] }
        }
      }

references_json:
  - filename: "POSTGRESQL_POOL_GUIDE.md"
    title: "PostgreSQL 连接池配置参考"
    content: |
      ## 推荐配置
      | 服务类型 | min_connections | max_connections | 说明 |
      |---------|----------------|-----------------|------|
      | API 网关 | 10 | 50 | 短连接为主 |
      | 业务服务 | 20 | 100 | 混合负载 |
      | 后台任务 | 5 | 30 | 低频长连接 |

      ## 常见问题
      - max_connections 超过 PostgreSQL `max_connections` 设置会导致连接失败
      - idle_in_transaction 连接会阻塞 VACUUM
      ...

# 测评用例见 tests/agent-resources/skills/db-pool-saturation-detector.yaml (§2.4)
```

### 2.2 Skill: `slow-query-classifier`

```yaml
# ============================================================
# Skill: slow-query-classifier
# 描述: 分类慢查询类型，给出针对性优化建议
# ============================================================
skill_name: "slow-query-classifier"
display_name: "慢查询分类器"
version: "1.0"
is_active: true

description: |
  根据 pg_stat_statements 或日志中的慢查询信息，分类慢查询类型并给出优化建议。
  触发条件：确认连接池饱和根因为 slow_query_blocking，需要进一步定位具体慢查询模式。

instructions_md: |
  ## 概述
  分析慢查询的模式特征，归类到以下类型之一，给出对应的优化方向。

  ## 步骤 1：特征提取
  从输入数据中提取：
  - 查询时长分布（p50/p95/p99）
  - 查询频率（calls/sec）
  - 查询类型（SELECT/INSERT/UPDATE/DELETE）
  - 涉及表名和行数
  - 是否使用索引

  ## 步骤 2：分类规则

  | 特征 | 类型 | 优先级 |
  |------|------|--------|
  | seq_scan + rows > 100000 | full_table_scan | P0 |
  | wait_event 含 "LWLock" 或 "lock" | lock_contention | P0 |
  | duration > 10s + rows > 50000 | large_transaction | P1 |
  | calls/sec > 100 + avg_duration < 100ms | n_plus_1 | P1 |
  | 多表 JOIN + no index on WHERE | missing_index | P2 |
  | 单表简单查询 + duration > 5s | statistics_outdated | P2 |
  | 不匹配任何模式 | unknown | P3 |

  ## 步骤 3：优化建议
  - full_table_scan: 为 WHERE/JOIN 列加索引；考虑分区表
  - lock_contention: 缩短事务；降低隔离级别；拆分大事务
  - large_transaction: 分批处理；使用 COPY 替代大量 INSERT
  - n_plus_1: 使用 JOIN 替代循环查询；启用批量加载
  - missing_index: 创建复合索引；运行 EXPLAIN ANALYZE 验证
  - statistics_outdated: 运行 ANALYZE；检查 autovacuum 配置

  ## 踩坑提醒
  - 单次慢查询 vs 高频快查询都可能耗尽连接池，关注 calls/sec × avg_duration 积
  - 索引不是万能药，过多索引会拖慢写入
  - 注意区分 "当前正在执行的慢查询" 和 "历史慢查询统计"

  ## 输出格式

      {
        "ok": true,
        "value": "<query_type>|<severity>|<optimization_direction>",
        "confidence": "high|medium|low",
        "details": {
          "query_type": "full_table_scan|lock_contention|large_transaction|n_plus_1|missing_index|statistics_outdated|unknown",
          "severity": "P0|P1|P2|P3",
          "optimization_direction": "创建索引|优化事务|分批处理|重构查询|更新统计信息",
          "affected_tables": ["table1", "table2"],
          "estimated_impact": "此查询占用 X% 的连接时间"
        }
      }

allowed_tools: "connection_tracker service_log_collector"

metadata_json:
  author: "hci-team"
  category: "database"
  tags: [database, slow-query, optimization, postgresql]

assets_json:
  - filename: "query_patterns.json"
    type: "data"
    content: |
      {
        "patterns": {
          "full_table_scan": {
            "indicators": ["seq_scan", "rows > 100000", "no index usage"],
            "optimization": "CREATE INDEX + 分区表"
          },
          "lock_contention": {
            "indicators": ["LWLock", "lock", "wait_event: transactionid"],
            "optimization": "缩短事务 + 降低隔离级别"
          },
          "large_transaction": {
            "indicators": ["duration > 10s", "rows > 50000"],
            "optimization": "分批处理 + COPY 替代 INSERT"
          },
          "n_plus_1": {
            "indicators": ["calls/sec > 100", "avg_duration < 100ms", "similar queries"],
            "optimization": "JOIN 替代循环 + 批量加载"
          }
        }
      }

# 测评用例见 tests/agent-resources/skills/slow-query-classifier.yaml
```

### 2.3 Skill YAML 字段说明

Skill YAML 的字段就是 `POST /api/v1/skills` 的请求体。

| 字段 | 中文名称 | 类型 | 必填 | 作用 | 示例 |
|------|---------|------|:--:|------|------|
| `skill_name` | 技能标识 | string(64) | ✅ | kebab-case 唯一标识。SOP variable_schema 中 `acquisition_tool` 通过此名查找 Skill。创建后不可修改 | `db-pool-saturation-detector` |
| `display_name` | 展示名称 | string(200) | ❌ | 管理台展示名，不注入 LLM prompt | `数据库连接池饱和度检测` |
| `description` | 触发描述 | string(1024) | ✅ | **Agent Discovery 阶段的唯一依据**（~100 tokens）。Agent 据此判断"是否需要激活此 Skill"。SOP 中 `acquisition_strategy: skill_call` 时，此描述帮助 LLM 确认应该调用哪个 Skill | `根据 db_pool_analyzer 的输出了判断数据库连接池饱和程度...` |
| `instructions_md` | 技能指令 | text | ❌ | **Skill 的核心**。Markdown 格式的分步推理指令（< 5000 tokens）。LLM 严格按此执行，包括步骤、规则表、踩坑提醒、输出格式模板。输出必须是 JSON | `## 步骤 1：饱和度分级\n\n \| utilization_pct \| waiting_requests \| 级别 \|...` |
| `compatibility` | 兼容性说明 | string(500) | ❌ | 环境依赖说明。不注入 LLM prompt，供运维查看 | `HCI v2.x+，依赖 db_pool_analyzer Tool 的输出` |
| `license` | 许可证 | string(100) | ❌ | 许可证标识 | `MIT` |
| `allowed_tools` | 授权工具 | text | ❌ | 空格分隔的预授权工具列表。创建/启用时校验引用的 Tool 存在且 `is_active=true`。Skill 执行时只能调用此列表内的工具 | `db_pool_analyzer` |
| `metadata_json` | 元数据 | JSONB | ❌ | `author`、`category`、`tags`。列表 API 支持按 category 过滤 | `{"author":"hci-team","category":"database","tags":["pool"]}` |
| `is_active` | 启用状态 | bool | ✅ | `false` 时 DynamicSkillRunner 找不到此 Skill，调用报错 | `true` |
| `assets_json` | 资源文件 | JSONB | ❌ | 内联资源文件。格式 `[{"filename":"...","type":"template\|schema\|data\|other","content":"..."}]`。当前未注入 LLM prompt（预留扩展点） | `[{"filename":"rules.json","type":"data","content":"{...}"}]` |
| `references_json` | 参考文档 | JSONB | ❌ | 内联参考文档。格式 `[{"filename":"...","title":"...","content":"..."}]`。当前未注入 LLM prompt（预留扩展点） | `[{"filename":"POOL_GUIDE.md","title":"连接池配置参考","content":"..."}]` |

**关键理解**：
- `description` 用于 **Discovery**——Agent 决定"要不要激活这个 Skill"
- `instructions_md` 用于 **Activation**——Agent 激活后严格按指令推理
- 两者加起来是渐进式加载：先看 100 tokens 的描述，确认需要才加载 5000 tokens 的完整指令

### 2.4 Skill 测评用例示例

```yaml
# tests/agent-resources/skills/db-pool-saturation-detector.yaml
# 文件名与 agent-resources/skills/db-pool-saturation-detector.yaml 对应

eval_type: "critical_decision"
stability_trials: 5
pass_threshold: 80

scoring:
  result_correctness:  { max_deduction: -100, evaluator: "deterministic" }
  process_compliance:   { max_deduction: -10,  evaluator: "deterministic" }
  efficiency:           { max_deduction: -10,  evaluator: "deterministic" }
  reasoning_quality:    { max_deduction: -20,  evaluator: "rubric" }

efficiency_baseline:
  max_latency_sec: 30
  max_tokens: 1500
  max_tool_calls: 2

# ── 触发条件 ──
trigger_cases:
  - case_id: "trigger-pos-critical"
    type: "positive"
    description: "utilization=98%, waiting=15 → 触发"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 98, waiting_requests: 15, connection_timeouts: 8, avg_wait_time_ms: 8500, active_connections: 48, idle_connections: 2, max_connections: 50 }
    expected:
      skill_triggered: true

  - case_id: "trigger-pos-warning"
    type: "positive"
    description: "utilization=72%, waiting=2 → 触发（warning 也触发）"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 72, waiting_requests: 2, connection_timeouts: 0, avg_wait_time_ms: 100, active_connections: 35, idle_connections: 10, max_connections: 50 }
    expected:
      skill_triggered: true

  - case_id: "trigger-neg-normal"
    type: "negative"
    description: "utilization=30%, waiting=0 → 不触发"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 30, waiting_requests: 0, connection_timeouts: 0, avg_wait_time_ms: 5, active_connections: 10, idle_connections: 20, max_connections: 50 }
    expected:
      skill_not_triggered: true

# ── 核心逻辑（每条分支 ≥1 用例） ──
core_logic_cases:
  - case_id: "logic-critical-slow-query"
    type: "positive"
    description: "critical + avg_wait > 5000ms → slow_query_blocking"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 96, waiting_requests: 12, avg_wait_time_ms: 8500, connection_timeouts: 15, active_connections: 48, idle_connections: 2, max_connections: 50 }
    expected:
      output_value: "critical|slow_query_blocking|immediate"
      output_confidence: "high"
      details:
        saturation_level: "critical"
        inferred_cause: "slow_query_blocking"
        urgency: "immediate"
      supporting_evidence_not_empty: true
      output_is_valid_json: true

  - case_id: "logic-critical-config"
    type: "positive"
    description: "critical + idle=0, max=30, waiting=20 → config_insufficient"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 100, waiting_requests: 20, avg_wait_time_ms: 2000, connection_timeouts: 25, active_connections: 30, idle_connections: 0, max_connections: 30 }
    expected:
      output_value: "critical|config_insufficient|immediate"
      details: { saturation_level: "critical", inferred_cause: "config_insufficient" }

  - case_id: "logic-high-traffic-surge"
    type: "positive"
    description: "high + waiting 持续增长 → traffic_surge"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 84, waiting_requests: 8, avg_wait_time_ms: 1200, connection_timeouts: 2, active_connections: 42, idle_connections: 3, max_connections: 50 }
        historical_trend: { waiting_requests: "growing" }
    expected:
      output_value: "high|traffic_surge|high"

  - case_id: "logic-warning-leak"
    type: "positive"
    description: "warning + idle 持续增长 → connection_leak"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 72, waiting_requests: 3, avg_wait_time_ms: 300, connection_timeouts: 0, active_connections: 15, idle_connections: 28, max_connections: 50 }
        historical_trend: { idle_connections: "growing" }
    expected:
      output_value: "warning|connection_leak|medium"

  - case_id: "logic-normal"
    type: "positive"
    description: "所有指标正常 → normal"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 20, waiting_requests: 0, avg_wait_time_ms: 5, connection_timeouts: 0, active_connections: 10, idle_connections: 15, max_connections: 50 }
    expected:
      output_value: "normal|normal|low"
      output_confidence: "high"

  - case_id: "logic-empty-input"
    type: "negative"
    description: "输入为空 → 返回 error"
    input:
      context_variables:
        db_pool_metrics: {}
    expected:
      output_ok: false
      output_error_not_empty: true

# ── 产物质量 ──
output_quality_cases:
  - case_id: "output-schema-valid"
    type: "positive"
    description: "输出 Schema 完整性"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 96, waiting_requests: 12, avg_wait_time_ms: 8500, connection_timeouts: 15, active_connections: 48, idle_connections: 2, max_connections: 50 }
    expected:
      output_is_valid_json: true
      output_has_keys: [ok, value, confidence, details]
      details_has_keys: [saturation_level, inferred_cause, urgency]
      saturation_level_in: [critical, high, warning, normal]
      confidence_in: [high, medium, low]
      response_not_contains: ["password", "secret", "api_key"]

# ── 异常容错 ──
robustness_cases:
  - case_id: "error-incomplete-metrics"
    type: "negative"
    description: "只提供部分指标 → 尽力判断 + 降低 confidence"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 90, waiting_requests: 8 }
    expected:
      output_ok: true
      confidence_in: [medium, low]   # 数据不完整应降低置信度

  - case_id: "error-invalid-values"
    type: "negative"
    description: "指标值非法 → 返回 error"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 150, waiting_requests: -5 }
    expected:
      output_ok: false
      output_error_contains: ["非法值", "超出范围", "invalid"]

  - case_id: "error-large-input"
    type: "negative"
    description: "大数据量输入不超时"
    input:
      context_variables:
        db_pool_metrics: { utilization_pct: 96, waiting_requests: 12, avg_wait_time_ms: 8500, connection_timeouts: 15, active_connections: 48, idle_connections: 2, max_connections: 50 }
        extra_noise: "x" * 10000     # 掺杂无关数据
    expected:
      execution_time_ms_lt: 30000    # 30s 内必须返回
      no_crash: true

# ── Rubric 评分 ──
rubric:
  observation_points:
    - "饱和度级别判定是否基于规则表而非编造"
    - "根因推断是否有 supporting_evidence 支撑"
    - "confidence 是否合理反映数据完整度"
    - "历史趋势数据是否被正确利用"
  scoring:
    classification_accuracy: "0-100"
    reasoning_quality: "0-100"
    is_false_positive: "bool"

# ── 基线 ──
baselines:
  - baseline_id: "sat-baseline-critical"
    description: "critical + slow_query 场景基线"
    session_id: "sat-baseline-001"
    message_id: "sat-msg-001"
    expected_trace:
      thought_contains: ["utilization_pct ≥ 95%", "critical"]
      output_schema_matches: true
      latency_ms: 5000
      tokens: 800
```


## 三、SOP 模版

SOP 的创作格式就是 **Markdown**——和当前 Web UI 上传的 `.md` 文件完全一致。
项目已有的 parser（`kb-service/app/services/sop_parser.py`）认识以下标题约定，
会自动从 Markdown 中解析出 `tree_json`（决策树）和 `variable_schema`（变量声明）。

也就是说：**这份 Markdown 就是 SOP 的源码。** 写完直接上传/提交即可，无需再手工构造 YAML。

### 3.1 SOP: `sop-db-pool-exhaustion.md`

SOP 源文件 = **Markdown 正文** + **HTML 注释形式的测评元数据**（parser 忽略，测评系统读取）。

```markdown
<!--
  @sop_meta:
    source_id: "sop-db-pool-exhaustion"
    category_id: "database-001"
    eval_type: "critical_decision"
    stability_trials: 5
    pass_threshold: 80
    owner: "hci-team"

  @baselines:
    n-1-1: { session_id: "sop-pool-001", message_id: "sop-pool-msg-001", desc: "慢查询阻塞分支基线" }
    n-1-2: { session_id: "sop-pool-002", message_id: "sop-pool-msg-002", desc: "配置不足分支基线" }
    n-1-3: { session_id: "sop-pool-003", message_id: "sop-pool-msg-003", desc: "网络/应用层分支基线" }
-->

# 数据库连接池耗尽排障

## 变量声明

| 变量名 | 类型 | 来源 | 说明 | 依赖 | 输出路径 | 参数模板 | 表达式 |
|--------|------|------|------|------|---------|---------|--------|
| service_name | string | user_input | 出现连接超时报错的服务名称。LLM 需先向用户询问 | - | - | - | - |
| node_ip | string | env:node_ip | 告警来源节点 IP。SOP 初始化阶段从告警上下文注入 | - | - | - | - |
| db_pool_status | object | tool:db_pool_analyzer | 数据库连接池当前状态指标（活跃/空闲/等待/超时/利用率） | service_name, node_ip | - | `{"node_ip":"{node_ip}","service_name":"{service_name}"}` | - |
| pool_saturation | string | skill:db-pool-saturation-detector | 智能判定连接池饱和度级别（critical/high/warning/normal）及根因方向 | db_pool_status | value | - | - |
| slow_query_type | string | skill:slow-query-classifier | 分类慢查询类型（全表扫描/锁等待/大事务/N+1/缺索引），给出优化方向 | pool_saturation | value | - | - |
| log_snapshot | object | tool:service_log_collector | 故障时间段内与连接异常相关的服务日志快照 | service_name, node_ip | - | `{"node_ip":"{node_ip}","service_name":"{service_name}","keywords":["connection timeout","pool exhausted","could not get connection"]}` | - |
| connection_data | object | tool:connection_tracker | 当前活跃数据库连接的详细信息（含 PID、查询内容、等待事件） | node_ip | - | `{"node_ip":"{node_ip}","filter_state":"active","min_duration_sec":5}` | - |
| blocking_pid | int | json_extract | 从 connection_data 中提取阻塞时间最长的查询 PID | connection_data | - | - | `$.parsed.top_queries[0].pid` |

<!-- @baseline-ref: n-1-1 -->
## 连接池饱和——慢查询阻塞

### 前置条件

- `{pool_saturation}` 判定为 critical 或 high，且 inferred_cause 为 slow_query_blocking（filter, text）
- `{log_snapshot}` 中 connection timeout 或 pool exhausted 关键字命中次数 > 10（filter, command）

## 诊断方法

### 页面判断方法

- 检查数据库监控面板的 QPS 和慢查询数量趋势图

### 分析步骤

<!--
  执行顺序与依赖:
  [1] → {connection_data}, {blocking_pid}
  [2] → {log_snapshot}
  [1][2] 可并行，数据源独立
  [3] 不依赖 [1][2]，{db_pool_status} 已在变量池中
  [4] 依赖 [1]（{connection_data} 输入 → skill → {slow_query_type}）
  [5] 依赖 [4]（有了分类结果才能对照基线）
  [6] 依赖 [1][3]（综合两个数据源）
-->

1. **[可并行]** 调用 `connection_tracker` 工具（参数 `node_ip={node_ip}`, `filter_state=active`），从返回的 `{connection_data}` 中定位阻塞时间最长的查询（pid=`{blocking_pid}`），记录查询内容、等待事件和持续时间
2. **[可并行]** 调用 `service_log_collector` 工具（参数 `node_ip={node_ip}`, `service_name={service_name}`, `keywords=['duration:', 'slow query']`），从 `{log_snapshot}` 中统计 connection timeout 的时间分布，确认故障起始时间和持续时长
3. **[可并行]** 读 `{db_pool_status}`（已在变量池中），确认连接池饱和度：active_connections / max_connections 比值、waiting_requests 队列长度
4. **[依赖步骤 1]** `{connection_data}` 已就绪 → `{slow_query_type}` 由 skill 自动得出。根据分类结果（全表扫描/锁等待/大事务），与 `{connection_data}` 中的实际查询内容交叉验证
5. **[依赖步骤 4]** 对照基线，确认该查询是否为新增或近期变化
6. **[依赖步骤 1,3]** 综合 `{connection_data}`（阻塞查询详情）和 `{db_pool_status}`（饱和度指标），评估该查询对连接池的影响占比（waiting_requests / max_connections）

### 可能原因

- 新上线的功能引入了全表扫描查询
- 统计数据过期导致优化器选择了错误的执行计划
- 批量操作未分批执行，单事务过大阻塞其他请求

## 解决方案

### 快速恢复

- 终止阻塞查询: `SELECT pg_terminate_backend({blocking_pid})`
- 临时增加连接池 max_connections 争取排查时间
- 重启受影响的微服务实例（仅短期缓解，问题会复现）

### 彻底修复

- 为全表扫描查询添加索引: `CREATE INDEX CONCURRENTLY ...`
- 优化大事务为分批提交（每批 ≤ 1000 行）
- 调整 autovacuum 参数确保统计信息及时更新
- 添加慢查询监控告警（阈值: duration > 5s, calls > 10/min）

<!-- @baseline-ref: n-1-2 -->
## 连接池饱和——配置不足

### 前置条件

- `{pool_saturation}` 判定 inferred_cause 为 config_insufficient（filter, text）
- `{db_pool_status}` 中 max_connections 小于业务实际需求，且 utilization_pct > 90%（filter, text）

## 诊断方法

### 分析步骤

<!--
  [1] connection_tracker → 各应用连接数统计
  [2] {db_pool_status} 已在变量池中，无需执行命令
  [3] 依赖 [1]+[2]（两边数据对比）
  [4] 依赖 [3]
  [5] 不依赖前面步骤
-->

1. **[可并行]** 调用 `connection_tracker` 工具（参数 `node_ip={node_ip}`, `filter_state=all`, `group_by=application`），从返回结果中统计各应用的实际连接数（按 application 分组）
2. **[可并行]** 读 `{db_pool_status}`（已在变量池中），确认当前配置：max_connections 上限、active_connections + waiting_requests 实际需求
3. **[依赖步骤 1,2]** 对比各应用实际连接需求与 `{db_pool_status}` 的 max_connections，判断是否不足及缺口大小（参考 POSTGRESQL_POOL_GUIDE.md 推荐值）
4. **[依赖步骤 3]** 若 `{db_pool_status}` 的 max_connections 已接近 PostgreSQL 全局上限，检查全局 max_connections 是否还有余量可分配
5. **[可并行]** 确认近期是否有与 `{service_name}` 相关的服务扩容或业务量增长

### 可能原因

- 服务扩容后连接池配置未同步调整
- 业务自然增长导致连接需求超过初始配置
- 其他服务占用过多连接配额

## 解决方案

### 快速恢复

- 临时提升连接池 max_connections 至推荐值
- 减少其他非关键服务的连接数分配

### 彻底修复

- 根据各服务实际负载调整连接池配置并写入 Helm values
- 引入 PgBouncer 连接池中间件
- 建立连接数监控与自动扩容机制

<!-- @baseline-ref: n-1-3 -->
## 连接池正常——应用层/网络问题

### 前置条件

- `{pool_saturation}` 判定为 normal 或 warning 且无连接池级别问题（filter, text）
- `{db_pool_status}` 中 utilization_pct < 70% 且 waiting_requests = 0（filter, text）

## 诊断方法

### 分析步骤

<!--
  [1] {db_pool_status} 已在变量池中，无需执行命令
  [2] ping 和 [3] curl 可并行，数据源独立
  [4] 依赖 [2][3]（网络检测结果 + 日志 交叉验证）
  [5][6] 不依赖前面步骤
-->

1. 读 `{db_pool_status}`（已在变量池中），确认指标正常（utilization_pct < 70%, waiting_requests = 0），排除连接池瓶颈
2. **[可并行]** 执行 bash 命令 `ping -c 10 {node_ip}`，检查从 `{node_ip}` 到数据库的网络延迟和丢包率
3. **[可并行]** 执行 bash 命令 `curl -w '@curl-format.txt' -o /dev/null -s http://{node_ip}:8080/health`，检查 `{service_name}` HTTP health 端口的响应时间和状态码
4. **[依赖步骤 2,3]** 从 `{log_snapshot}` 中确认错误日志的时间分布是否与网络检测结果（延迟突变/丢包/HTTP 超时）吻合
5. **[可并行]** 检查 HTTP client 超时配置（connect timeout / read timeout）是否过短
6. **[可并行]** 检查负载均衡器后端健康检查状态和 DNS 解析延迟

### 可能原因

- 应用层 HTTP client 超时配置过短（connect timeout < 1s）
- 网络链路抖动导致 TCP 连接建立超时
- 负载均衡器后端健康检查失败导致流量分发异常

## 解决方案

### 快速恢复

- 临时增加应用层超时时间至 5s
- 从负载均衡器摘除故障节点

### 彻底修复

- 调整 HTTP client 超时配置（connect: 3s, read: 30s）
- 排查网络链路问题（检查交换机/防火墙日志）
- 优化健康检查参数（间隔 5s → 10s，阈值 3 次 → 5 次）
```

> **说明**：
> - **`<!-- @sop_meta -->`**：HTML 注释形式的测评元数据，放在文件最顶部。SOP parser 忽略 HTML 注释，测评系统解析此块获取 `source_id`、`eval_type`、`stability_trials`、基线引用。不污染 Markdown 正文。
> - **`<!-- @baseline-ref: n-1-1 -->`**：每个分支节点前的基线标记，测评时按 `node_id` 匹配对应的预期 Trace。
> - **变量表每列必须填值**：不需要的列填 `-`，不要留空。让 reviewer 一眼看出是否遗漏配置。
> - **`### 前置条件` 条目标注 `(filter|priority, text|command)`**：parser 用此区分路由条件类型。
> - 以上 Markdown 就是当前 Web UI 上传 `.md` 文件时的格式。

### 3.1.1 SOP 变量声明表格——列说明

Parser 识别的列名（中文别名，大小写不敏感）：

| 列名 | 必填 | 别名 | 作用 | 示例值 |
|------|:--:|------|------|--------|
| **变量名** | ✅ | `name`, `变量` | 变量标识符，LLM 通过 `sop_request_variable(variable_name)` 引用 | `service_name` |
| **类型** | ✅ | `type` | 变量类型。`string` \| `int` \| `ip` \| `object` \| `boolean`。用于前端表单渲染和参数校验 | `string` |
| **来源** | ✅ | `source` | **核心列**。取值策略。`user_input`=用户填写, `env_injection`=环境注入, `tool:<name>`=调用Tool, `skill:<name>`=调用Skill, `sop_default:<val>`=默认值, `llm_inference`=LLM推理, `derived`=表达式计算, `json_extract`=JSONPath提取 | `tool:db_pool_analyzer` |
| **说明** | ❌ | `description` | 变量用途描述。注入到 `sop_request_variable` 的返回结果中，帮助 LLM 理解为什么需要此变量 | `出现连接超时报错的服务名称` |
| **依赖** | ❌ | `depends_on`, `前置变量` | 逗号分隔的前置变量名。当前变量获取前，这些变量必须先就绪 | `service_name, node_ip` |
| **输出路径** | ❌ | `output_path` | Tool/Skill 返回 JSON 中提取变量值的路径。如 `parsed.utilization_pct`、`value` | `parsed.utilization_pct` |
| **参数模板** | ❌ | `acquisition_args_template`, `工具参数`, `tool_args` | 当来源为 `tool:<name>` 时，传给 Tool 的参数 JSON。支持 `{变量名}` 占位符引用变量池中的值 | `{"node_ip":"{node_ip}"}` |
| **表达式** | ❌ | `expression`, `派生表达式` | 当来源为 `derived` 时的计算表达式，或 `json_extract` 时的 JSONPath | `contains(alert_type, 'disk')` |

**列顺序自由**：Parser 按列名匹配而非列位置，可以任意排列。只有 `变量名`、`类型`、`来源` 必填，其余按需添加。

### 3.1.2 SOP Markdown 标题约定——Parser 如何解析决策树

Parser 根据 Markdown 标题层级和关键字自动构建 `tree_json`：

| Markdown 写法 | Parser 行为 | 生成的 JSON |
|--------------|------------|------------|
| `# 数据库连接池耗尽排障` | 根节点（`level: 1`） | `{"id":"n-1","title":"数据库连接池耗尽排障","level":1}` |
| `## 连接池饱和——慢查询阻塞` | 子节点（`level: 2`） | `{"id":"n-1-1","title":"连接池饱和——慢查询阻塞","level":2}` |
| `### 前置条件` | 节点的前置条件列表 | `prerequisite_items: [{description:"...", type:"filter\|priority", content_type:"text\|command"}]` |
| `## 诊断方法` | 叶子节点标记（diagnosis） | `diagnosis: {analysis_steps:[], possible_causes:[]}` |
| `## 解决方案` | 叶子节点标记（solution） | `solution: {quick_recovery:[], thorough_fix:[]}` |
| `### acli 命令` | **⚠️ 当前 Parser 要求必填**（`min_length=1`）。模版建议命令内联到分析步骤中，但在 Parser 放宽约束前，诊断节点仍需至少一条 acli 命令。无实际命令时可用占位命令 `echo 'diagnosis via variable pool'` | `diagnosis.acli_methods` |
| `### 分析步骤` | 诊断执行流程。**命令直接内联在步骤中**（如"执行 `xxx` 命令，从结果中..."）。Parser 放开 acli_methods 约束后，不再需要单独的命令节 | `diagnosis.analysis_steps` |
| `### 页面判断方法` | 可选。UI/监控面板辅助观察 | `diagnosis.page_methods` |
| `### 可能原因` | 诊断节点下的根因列表 | `diagnosis.possible_causes` |
| `### 快速恢复` | 解决方案下的止血步骤 | `solution.quick_recovery` |
| `### 彻底修复` | 解决方案下的根除步骤 | `solution.thorough_fix` |

**关键规则**：
- 非关键字标题（不是"变量声明""诊断方法""解决方案""前置条件"）→ 识别为决策树节点
- 有子标题的节点 → `type: branch`
- 有 `## 诊断方法` 或 `## 解决方案` 的节点 → `type: leaf`（diagnosis / solution）
- 前置条件中 `type: filter` 表示匹配条件，`type: priority` 表示排序条件

**分析步骤中命令的写法规范**：

Agent 的 13 个默认工具全部来自 `tool_definition` 表，启动时加载到 `TOOL_REGISTRY`，转为 OpenAI Function Calling 格式传给 LLM。**LLM 每步推理都能看到所有工具，靠 `description` 文本决定调用哪个**——没有硬编码的路由规则。

分析步骤中的"执行命令"措辞直接决定了 LLM 选哪个工具：

| 措辞 | LLM 匹配到的工具 | 实际行为 |
|------|-----------------|---------|
| `调用 X 工具（参数 ...）` | 在 Function Calling 列表中搜索 `name=X` 的工具 | 匹配成功 → 结构化调用；匹配失败 → LLM 可能报错或误用 `bash_exec` |
| `执行 acli 命令 \`acli ...\`` | `acli_exec` | Agent 把 acli 命令字符串转发到目标节点执行 |
| `执行 bash 命令 \`cmd\`` | `bash_exec` | Agent 在 host 容器中执行 shell 命令 |
| `读 {variable_name}` | 无（不调工具） | 直接使用 `sop_request_variable` 已获取到变量池的值 |

**SOP 可用的默认工具清单**（13 个，来自 `database/seeds/01_tool_definitions.sql`）：

> 以下按功能分类。DB 中 `category` 字段实际只有 `acli` 和 `sop` 两个值。

```
SOP 导航（3个，仅 SOP 模式可见）:
  get_sop_node         — 获取决策树节点内容
  sop_advance          — 推进到子节点
  sop_request_variable — 请求获取变量值

数据采集（4个，DB category=acli）:
  get_active_alerts    — 查询活跃告警列表
  get_failed_tasks     — 查询失败任务（可按关键字/错误码/时间过滤）
  get_vm_list          — 查询虚拟机列表
  get_cluster_detail   — 查询集群详情

命令执行（2个，DB category=acli）:
  bash_exec            — 在目标节点容器中执行 Linux 命令
  acli_exec            — 执行 acli 命令

诊断插件（4个，DB category=acli）:
  acli_plugin_asys         — 主机系统健康检查
  acli_plugin_netdoctor    — 网络综合诊断
  acli_plugin_vm_start     — 虚拟机开机失败诊断
  acli_plugin_vm_suspend   — 虚拟机异常挂起诊断
```

> **注意**：本模版中的 `connection_tracker`、`service_log_collector`、`db_pool_analyzer` 是虚构工具，用于演示 SOP 结构。实际 SOP 中引用的工具必须在 `tool_definition` 表中存在。如果需要的诊断能力没有对应工具，要么新增 Tool，要么把逻辑拆到 Skill 的 `instructions_md` 中。


### 3.1.3 表达式变量详解——`表达式` 列怎么用

`表达式` 列配合 `来源` 列使用，只在两种策略下生效：

| 来源策略 | 表达式的作用 | 表达式语法 | 示例 |
|---------|------------|-----------|------|
| `derived` | 从其他变量值**计算**出新变量值 | 自研 DSL（见下方） | `contains(alert_type, 'disk') ? 'storage' : 'other'` |
| `json_extract` | 从 JSON 数据中**提取**字段值 | JSONPath | `$.disks[?(@.device=='{disk_name}')].vendor` |
| 其他策略 | 不使用 | 填 `-` | `-` |

#### 策略一：`derived`（表达式计算）

`derived` 变量不调用任何 Tool/Skill，而是直接用表达式计算。表达式引擎（`_evaluate_derived_expression`）支持的语法：

**1. 变量引用**：直接写变量名，从变量池取值
```
alert_type          → "disk_io_error"
db_pool_status      → {"utilization_pct": 96, ...}
```

**2. 函数调用**：
| 函数 | 参数 | 说明 | 示例 | 结果 |
|------|------|------|------|------|
| `contains(a, b)` | 2 个 | b 是否在 a 中出现 | `contains('disk_io_error', 'disk')` | `true` |
| `equals(a, b)` | 2 个 | a 是否等于 b | `equals(alert_type, 'disk_error')` | `true` |
| `starts_with(a, b)` | 2 个 | a 是否以 b 开头 | `starts_with('vm_start_failure', 'vm')` | `true` |
| `ends_with(a, b)` | 2 个 | a 是否以 b 结尾 | `ends_with('disk_io_error', 'error')` | `true` |
| `not(a)` | 1 个 | 逻辑取反 | `not(contains(alert_type, 'disk'))` | `false` |
| `split(a, b)` | 2 个 | 用分隔符 b 拆分字符串 a | `split('disk_io_error', '_')` | `["disk","io","error"]` |
| `join(a, b)` | 2 个 | 用分隔符 b 连接列表 a | `join(split('a_b_c', '_'), '-')` | `"a-b-c"` |

**3. 三元表达式**：`条件 ? 真值 : 假值`
```
contains(alert_type, 'disk') ? 'storage' : 'other'
equals(pool_saturation, 'critical') ? 'P0' : 'P1'
```

**4. 字面量**：`true` / `false` / `unknown` / `null` / `none` / `123` / `'字符串'` / `"字符串"`

**实际例子——给 SOP 新增一条 derived 变量**：

| 变量名 | 类型 | 来源 | 说明 | 依赖 | 输出路径 | 参数模板 | 表达式 |
|--------|------|------|------|------|---------|---------|--------|
| fault_category | string | derived | 根据告警类型推导故障大类 | alert_type | - | - | `contains(alert_type, 'disk') ? 'storage' : contains(alert_type, 'network') ? 'network' : 'other'` |

LLM 调用 `sop_request_variable("fault_category")` → 引擎执行表达式 → 直接返回 `"storage"`，无需调用 Tool。

#### 策略二：`json_extract`（JSON 提取）

当 Tool 输出是 JSON 格式（如 `db_pool_analyzer` 返回的 `parsed` 字段），可以用 JSONPath 从中提取子字段，避免把整个大 JSON 塞给 LLM。

**语法**：标准 JSONPath，支持 `{变量名}` 占位符（先用变量池中的值填充，再执行 JSONPath 查询）。

**实际例子**：

| 变量名 | 类型 | 来源 | 说明 | 依赖 | 输出路径 | 参数模板 | 表达式 |
|--------|------|------|------|------|---------|---------|--------|
| active_connections | int | json_extract | 从连接池指标中提取活跃连接数 | db_pool_status | - | - | `$.parsed.active_connections` |
| disk_vendor | string | json_extract | 从 SMART 数据中提取指定磁盘的厂商 | smartctl_json, disk_name | - | - | `$.disks[?(@.device=='{disk_name}')].vendor` |
| cpu_util | int | json_extract | 从系统指标中提取 CPU 利用率 | system_metrics | - | - | `$.cpu.utilization_pct` |

**执行流程**（以 `disk_vendor` 为例）：
1. 引擎检查 `depends_on` → `smartctl_json` 和 `disk_name` 必须先有值
2. 从 Redis 缓存或变量池读取 `smartctl_json` 的完整 JSON（避免截断）
3. 将 `{disk_name}` 替换为变量池中的实际值 → `$.disks[?(@.device=='sda')].vendor`
4. 执行 JSONPath 查询 → 返回 `"Intel"`

#### 变量表完整示例

结合两种表达式，连接池排障 SOP 可以补上以下 derived 和 json_extract 变量：

| 变量名 | 类型 | 来源 | 说明 | 依赖 | 输出路径 | 参数模板 | 表达式 |
|--------|------|------|------|------|---------|---------|--------|
| service_name | string | user_input | 出现连接超时报错的服务名称 | - | - | - | - |
| node_ip | string | env:node_ip | 告警来源节点 IP | - | - | - | - |
| db_pool_status | object | tool:db_pool_analyzer | 连接池当前状态指标 | service_name, node_ip | - | `{"node_ip":"{node_ip}","service_name":"{service_name}"}` | - |
| pool_saturation | string | skill:db-pool-saturation-detector | 连接池饱和度判定 | db_pool_status | value | - | - |
| slow_query_type | string | skill:slow-query-classifier | 慢查询类型分类 | pool_saturation | value | - | - |
| log_snapshot | object | tool:service_log_collector | 异常日志快照 | service_name, node_ip | - | `{"node_ip":"{node_ip}","service_name":"{service_name}","keywords":["connection timeout","pool exhausted"]}` | - |
| is_critical | boolean | derived | 连接池是否处于 critical 级别 | pool_saturation | - | - | `equals(pool_saturation, 'critical\|slow_query_blocking\|immediate')` |
| utilization_pct | int | json_extract | 提取连接池利用率百分比 | db_pool_status | - | - | `$.parsed.utilization_pct` |
| waiting_count | int | json_extract | 提取等待队列长度 | db_pool_status | - | - | `$.parsed.waiting_requests` |
| urgency_level | string | derived | 综合判定紧急程度 | is_critical, waiting_count | - | - | `equals(is_critical, 'true') ? 'P0' : waiting_count > 5 ? 'P1' : 'P2'` |

### 3.2 Trace 示例——SOP 完整执行轨迹

以下是以 `sop-db-pool-exhaustion` SOP 为例，Agent 从 S0 识别到走到 solution 的完整 ReAct 推理轨迹（JSONL 格式，每行一个事件）：

```jsonl
{"type":"system","content":"用户报告: api-gateway 服务大量 502 错误，日志出现 'connection timeout'"}
{"type":"s0_triage","category":"database","confidence":0.94,"matched_sop":"sop-db-pool-exhaustion","reason":"关键词 connection timeout + 502 匹配数据库连接池故障模式"}

{"type":"thought","content":"S0 已将工单分类为 database 并匹配到 SOP「数据库连接池耗尽排障」。我需要先获取 SOP 根节点，了解排查入口。"}
{"type":"tool_call","name":"get_sop_node","args":{"node_id":"n-1"},"id":"call_01","timestamp":"2026-06-29T10:00:01Z"}
{"type":"tool_result","name":"get_sop_node","id":"call_01","content":{"node_id":"n-1","type":"branch","title":"数据库连接池耗尽排障","children":[{"node_id":"n-1-1","title":"连接池饱和——慢查询阻塞","prerequisites":["{pool_saturation} 判定为 critical 或 high，且 inferred_cause 为 slow_query_blocking","{log_snapshot} 中 connection timeout 命中次数 > 10"]},{"node_id":"n-1-2","title":"连接池饱和——配置不足","prerequisites":["{pool_saturation} 判定 inferred_cause 为 config_insufficient","{db_pool_status} 中 max_connections 小于业务实际需求"]},{"node_id":"n-1-3","title":"连接池正常——应用层/网络问题","prerequisites":["{pool_saturation} 判定为 normal 或 warning 且无连接池级别问题","{db_pool_status} 中 utilization_pct < 70% 且 waiting_requests=0"]}],"required_variables":[{"name":"service_name","acquisition_strategy":"user_input"},{"name":"node_ip","acquisition_strategy":"env_injection"},{"name":"db_pool_status","acquisition_strategy":"tool_call","acquisition_tool":"db_pool_analyzer","depends_on":["service_name","node_ip"]}],"preferred_next_steps":[{"tool":"sop_request_variable","args":{"variable_name":"service_name"},"reason":"变量 'service_name' 需通过用户输入获取","priority":"high"}]},"timestamp":"2026-06-29T10:00:03Z"}

{"type":"thought","content":"根节点 n-1 有三个子分支。必须先获取变量 service_name 才能继续。根据 preferred_next_steps，先向用户询问故障服务名。"}
{"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"service_name","reason":"需要确认出现连接超时报错的服务名称"},"id":"call_02","timestamp":"2026-06-29T10:00:05Z"}
{"type":"tool_result","name":"sop_request_variable","id":"call_02","content":{"needs_input":true,"variable_name":"service_name","kind":"variable_input","message":"请提供出现连接超时报错的服务名称"},"timestamp":"2026-06-29T10:00:06Z"}

{"type":"user_response","content":"api-gateway","timestamp":"2026-06-29T10:00:15Z"}

{"type":"thought","content":"用户提供了 service_name=api-gateway。接下来获取 node_ip（环境注入，无需用户输入），然后获取 db_pool_status。"}
{"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"node_ip","reason":"需要确认告警来源节点 IP"},"id":"call_03","timestamp":"2026-06-29T10:00:17Z"}
{"type":"tool_result","name":"sop_request_variable","id":"call_03","content":{"ok":true,"value":"192.168.1.100","source":"env_injection"},"timestamp":"2026-06-29T10:00:17Z"}

{"type":"thought","content":"node_ip=192.168.1.100 已就绪。现在 db_pool_status 的依赖（service_name, node_ip）都满足了，可以获取连接池状态。"}
{"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"db_pool_status","reason":"需要确认数据库连接池当前状态"},"id":"call_04","timestamp":"2026-06-29T10:00:19Z"}
{"type":"tool_result","name":"sop_request_variable","id":"call_04","content":{"ok":true,"value":{"active_connections":48,"idle_connections":2,"max_connections":50,"waiting_requests":12,"avg_wait_time_ms":8500,"connection_timeouts":15,"utilization_pct":96},"source":"tool_call","tool":"db_pool_analyzer"},"timestamp":"2026-06-29T10:00:23Z"}

{"type":"thought","content":"db_pool_status 显示 utilization_pct=96%, waiting_requests=12, avg_wait_time_ms=8500。这是严重的连接池饱和。接下来获取 pool_saturation 让 Skill 判定级别。"}
{"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"pool_saturation","reason":"需要智能判定连接池饱和度级别和根因方向"},"id":"call_05","timestamp":"2026-06-29T10:00:25Z"}
{"type":"tool_result","name":"sop_request_variable","id":"call_05","content":{"ok":true,"value":"critical|slow_query_blocking|immediate","source":"skill_call","skill_name":"db-pool-saturation-detector","details":{"saturation_level":"critical","inferred_cause":"slow_query_blocking","urgency":"immediate","supporting_evidence":["utilization_pct=96% > 95%","waiting_requests=12 >= 10","avg_wait_time_ms=8500 > 5000"]}},"timestamp":"2026-06-29T10:00:28Z"}

{"type":"thought","content":"pool_saturation=critical，根因 slow_query_blocking。对照三个子分支的 prerequisites：n-1-1 要求「{pool_saturation} 为 critical 且 inferred_cause 为 slow_query_blocking」→ 匹配。还需要获取 log_snapshot 和 connection_data 才能推进。"}
{"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"log_snapshot","reason":"需要采集故障时段 connection timeout 日志确认时间线"},"id":"call_06","timestamp":"2026-06-29T10:00:30Z"}
{"type":"tool_result","name":"sop_request_variable","id":"call_06","content":{"ok":true,"value":{"matched_lines":250,"keywords_matched":{"connection timeout":200,"pool exhausted":50},"time_range":{"start":"10:00","end":"10:05"}},"source":"tool_call","tool":"service_log_collector"},"timestamp":"2026-06-29T10:00:34Z"}

{"type":"thought","content":"log_snapshot 显示最近 5 分钟有 200 条 connection timeout（>10 满足 n-1-1 前置条件）。现在 prerequisites 全部满足，可以推进到 n-1-1。"}
{"type":"tool_call","name":"sop_advance","args":{"target_node_id":"n-1-1","reasoning":"pool_saturation=critical，根因 slow_query_blocking，log_snapshot 确认 200 条 connection timeout 且 utilization_pct=96%"},"id":"call_07","timestamp":"2026-06-29T10:00:36Z"}
{"type":"tool_result","name":"sop_advance","id":"call_07","content":{"ok":true,"current_node_id":"n-1-1","node_type":"diagnosis","message":"已推进到：连接池饱和——慢查询阻塞","is_leaf":true,"content":"## 诊断方法\n\n### 分析步骤\n1. [可并行] 调用 connection_tracker 工具...\n2. [可并行] 调用 service_log_collector 工具...\n...","commands":[],"preferred_next_steps":[{"tool":"sop_request_variable","args":{"variable_name":"connection_data"},"reason":"变量 'connection_data' 需通过 connection_tracker 工具自动采集","priority":"high"},{"tool":"sop_request_variable","args":{"variable_name":"slow_query_type"},"reason":"变量 'slow_query_type' 需通过 slow-query-classifier Skill 自动采集","priority":"high"}]},"timestamp":"2026-06-29T10:00:37Z"}

{"type":"thought","content":"已到达叶子节点 n-1-1（慢查询阻塞诊断）。分析步骤 1 要求调用 connection_tracker 获取阻塞查询详情。按 preferred_next_steps，先获取 connection_data 和 slow_query_type。"}
{"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"connection_data","reason":"需要获取当前活跃连接的详细信息（PID、查询内容、等待事件）"},"id":"call_08","timestamp":"2026-06-29T10:00:39Z"}
{"type":"tool_result","name":"sop_request_variable","id":"call_08","content":{"ok":true,"value":{"total_connections":50,"active_count":48,"waiting_count":12,"top_queries":[{"pid":28461,"query_preview":"SELECT * FROM orders WHERE status='pending' ORDER BY created_at","duration_sec":22.5,"state":"active","wait_event":"DataFileRead"},{"pid":28503,"query_preview":"UPDATE inventory SET quantity=quantity-1 WHERE id=$1","duration_sec":8.2,"state":"active","wait_event":"transactionid"}]},"source":"tool_call","tool":"connection_tracker"},"timestamp":"2026-06-29T10:00:43Z"}

{"type":"thought","content":"connection_data 显示 pid=28461 的 SELECT 查询已运行 22.5s，wait_event=DataFileRead（磁盘读取等待），这是阻塞源头。第二个查询 pid=28503 在等待 transactionid 锁。现在获取 slow_query_type 分类。"}
{"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"slow_query_type","reason":"需要分类慢查询类型，确定优化方向"},"id":"call_09","timestamp":"2026-06-29T10:00:45Z"}
{"type":"tool_result","name":"sop_request_variable","id":"call_09","content":{"ok":true,"value":"full_table_scan|P0|创建索引","source":"skill_call","skill_name":"slow-query-classifier","details":{"query_type":"full_table_scan","severity":"P0","optimization_direction":"创建索引","affected_tables":["orders"],"estimated_impact":"此查询占用 45% 的连接时间"}},"timestamp":"2026-06-29T10:00:49Z"}

{"type":"thought","content":"slow_query_type=full_table_scan，P0 级别。综合所有数据：\n- db_pool_status: utilization_pct=96%, waiting=12, avg_wait=8500ms\n- connection_data: pid=28461 的 SELECT * FROM orders 全表扫描，22.5s，DataFileRead 等待\n- slow_query_type: full_table_scan，orders 表缺索引\n- log_snapshot: 5 分钟内 200 条 connection timeout\n\n结论：orders 表的全表扫描查询导致连接池耗尽。现在给出诊断结论和解决方案。"}
{"type":"thought","content":"分析步骤完成。诊断结论：orders 表上的 SELECT * FROM orders WHERE status='pending' 因缺少索引导致全表扫描（DataFileRead），单个查询持续 22.5s，阻塞其他 47 个活跃连接，引发 12 个等待请求和 200 次连接超时。按 SOP 解决方案：快速恢复→终止阻塞查询+临时扩容，彻底修复→为 orders.status 添加索引+优化 autovacuum。"}
{"type":"final_response","content":"## 故障诊断结论\n\n**根因**: orders 表缺少 status 字段索引，SELECT * FROM orders WHERE status='pending' 触发全表扫描（22.5s/次），耗尽数据库连接池。\n\n**关键证据**:\n- 连接池利用率 96%，12 个请求排队等待，平均等待 8.5s\n- pid=28461 的查询持续 22.5s，wait_event=DataFileRead（磁盘顺序读）\n- 慢查询分类: full_table_scan / P0\n- 5 分钟内 200 次 connection timeout\n\n### 解决方案\n\n**快速恢复**:\n1. 终止阻塞查询: `SELECT pg_terminate_backend(28461)`\n2. 临时增加连接池 max_connections 至 80 争取排查时间\n3. 重启 api-gateway 实例（短期缓解，问题会复现）\n\n**彻底修复**:\n1. 为 orders.status 添加索引: `CREATE INDEX CONCURRENTLY idx_orders_status ON orders(status)`\n2. 优化大事务为分批提交（每批 ≤ 1000 行）\n3. 调整 autovacuum 参数确保统计信息及时更新\n4. 添加慢查询监控告警（阈值: duration > 5s, calls > 10/min）","timestamp":"2026-06-29T10:00:55Z"}
```

#### Trace 关键节点解析

```
call_01: get_sop_node("n-1")
  → 返回根节点 + 3 个子分支 + required_variables + preferred_next_steps
  → LLM 知道需要先获取 service_name

call_02: sop_request_variable("service_name")
  → needs_input=true → 暂停，等待用户输入

call_03-04: service_name 就绪 → node_ip(env) → db_pool_status(tool:db_pool_analyzer)
  → 依赖链: service_name→node_ip→db_pool_status，按序获取

call_05: sop_request_variable("pool_saturation")
  → skill_call → DynamicSkillRunner 执行 LLM 推理
  → 返回 saturation_level=critical, cause=slow_query_blocking

call_06: sop_request_variable("log_snapshot")
  → tool_call → service_log_collector
  → 确认 5 分钟 200 条 timeout，满足 n-1-1 前置条件

call_07: sop_advance("n-1-1")
  → 变量门禁检查: pool_saturation + log_snapshot 已就绪 → 通过
  → 到达叶子节点，返回诊断步骤 + preferred_next_steps

call_08-09: 诊断步骤 1 和 4
  → connection_data(tool) → slow_query_type(skill)
  → 步骤 4 依赖步骤 1（connection_data 输入 → skill → slow_query_type）

final: LLM 综合所有变量给出诊断结论和解决方案
```

> **从 Trace 反向验证 SOP 模版**：
> - 变量声明中的 `depends_on` → 决定了 call_02→03→04 的顺序
> - 前置条件中的 `{pool_saturation}` `{log_snapshot}` → call_07 的门禁检查点
> - 分析步骤中的 `[依赖步骤 1]` → call_08 必须先于 call_09
> - 解决方案中的 `{blocking_pid}` → final_response 中引用了 pid=28461
> - Trace 中没有出现 `{slow_query_type}` 的显式引用 → 它在步骤 4 被 LLM 读取但 LLM 自己在 final 中用了（不是通过 `{}` 占位符），如果 SOP 解决方案中写了 `{slow_query_type}` 就应该在 final 中看到对应的值替换

### 3.3 测评用例（独立文件，不嵌入 SOP）

测评用例不嵌入 SOP Markdown（保持 Markdown 干净），而是放在独立的测试目录中，
按资源名索引：

```yaml
# tests/agent-resources/sops/sop-db-pool-exhaustion.yaml
# 文件名与 SOP 的 source_id 对应

eval_type: "critical_decision"
stability_trials: 5
pass_threshold: 80

efficiency_baseline:
  max_latency_sec: 600
  max_tokens: 10000
  max_tool_calls: 25

# 触发条件
trigger_cases:
  - case_id: "sop-trigger-pool-timeout"
    type: "positive"
    input:
      case_description: "api-gateway 大量 'connection timeout' 和 'pool exhausted' 错误，用户请求返回 502"
    expected:
      sop_triggered: true
      matched_category: "database"

  - case_id: "sop-trigger-neg-disk"
    type: "negative"
    input:
      case_description: "数据库服务器磁盘使用率 95%，需要清理空间"
    expected:
      sop_triggered: false

# 分支全覆盖
branch_coverage:
  - node_title: "连接池饱和——慢查询阻塞"
    cases:
      - case_id: "branch-slow-query-scan"
        description: "全表扫描导致连接池耗尽"
        input:
          context_variables:
            service_name: "api-gateway"
            node_ip: "192.168.1.100"
            db_pool_status: { utilization_pct: 96, waiting_requests: 12, avg_wait_time_ms: 8500 }
            pool_saturation: "critical|slow_query_blocking|immediate"
            slow_query_type: "full_table_scan|P0|创建索引"
        expected:
          tool_calls_contain: [connection_tracker]
          skill_calls_contain: [slow-query-classifier]
          solution_contains: ["终止阻塞查询", "autovacuum"]

  - node_title: "连接池饱和——配置不足"
    cases:
      - case_id: "branch-config-insufficient"
        description: "max_connections 配置不足"
        input:
          context_variables:
            service_name: "api-gateway"
            node_ip: "192.168.1.100"
            db_pool_status: { max_connections: 30, utilization_pct: 100, waiting_requests: 20 }
            pool_saturation: "critical|config_insufficient|immediate"
        expected:
          solution_contains: ["PgBouncer", "扩容机制"]

  - node_title: "连接池正常——应用层/网络问题"
    cases:
      - case_id: "branch-network-issue"
        description: "连接池正常但网络延迟导致超时"
        input:
          context_variables:
            db_pool_status: { utilization_pct: 24, waiting_requests: 0 }
            pool_saturation: "normal|normal|low"
        expected:
          diagnosis_contains: ["网络延迟", "HTTP 超时"]

# 路由稳定性
routing_cases:
  - case_id: "route-stability"
    description: "多轮对话保持 SOP 锁定"
    input:
      conversation_history:
        - user: "api-gateway 大量 502 错误"
        - assistant: "正在检查数据库连接池状态..."
        - user: "继续"
      sop_resume_context: { active: true, document_id: "sop-db-pool-exhaustion" }
    expected:
      routing_mode: "sop"
      no_fallback: true

# 异常容错
robustness_cases:
  - case_id: "error-tool-failure"
    description: "db_pool_analyzer 执行失败时优雅降级"
    input:
      mock_tool_failure: "db_pool_analyzer"
    expected:
      no_crash: true
      fallback_suggested: true

  - case_id: "error-variable-blocked"
    description: "必要变量未就绪时阻断推进"
    input:
      context_variables: {}
      advance_request: "连接池饱和——慢查询阻塞"
    expected:
      advance_blocked: true
      error_type: "missing_required_variables"

rubric:
  observation_points:
    - "排障路径是否严格遵循决策树的分支条件"
    - "变量获取是否按 depends_on 声明的依赖顺序"
    - "诊断是否引用具体数据指标（非泛泛的'可能有问题'）"
    - "解决方案是否区分快速恢复（止血）和彻底修复（根除）"
```

---

## 四、相关文档

- **[Agent 能力边界与演进方向](./agent-能力边界与演进方向.md)**：Tool / Skill / SOP / ReAct / Agent 五层的能力现状、不支持的功能、替代方案和扩展优先级 Roadmap
- **[Agent 测评与 GitOps 全生命周期方案](../03-测评与GitOps/agent-测评与GitOps方案.md)**：七维评分体系、三类评委、四场景用例设计、提示词与 ReAct 测评、CI 门禁触发矩阵、GitOps 全生命周期衔接

