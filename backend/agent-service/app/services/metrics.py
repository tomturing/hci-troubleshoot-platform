"""
Prometheus Metrics definition for Agent Reliability
Agent可靠性核心指标 (T4-2)

完整指标列表（符合任务清单 T4-2 要求）：
  - agent_tool_call_success_rate（工具执行成功率）
  - agent_tool_timeout_rate（超时率）
  - agent_hallucination_detected_total（幻觉检测计数）
  - agent_information_confidence_avg（平均信息置信度）
  - agent_unsupported_claim_rate（无证据结论率）
  - agent_mean_steps_to_resolution（平均解决步数）
"""

from prometheus_client import Counter, Gauge, Histogram

# ─── 1. 工具调用统计 ───────────────────────────────────────────────────────

# 工具调用总数（按工具名称和状态分类）
AGENT_TOOL_CALL_TOTAL = Counter(
    "agent_tool_call_total", "Total number of tool calls by the Agent", labelnames=["tool_name", "status"]
)

# 工具执行超时计数（T4-2: agent_tool_timeout_rate）
AGENT_TOOL_TIMEOUT_TOTAL = Counter(
    "agent_tool_timeout_total", "Total number of tool execution timeouts", labelnames=["tool_name"]
)

# 工具执行耗时分布（用于计算成功率趋势）
AGENT_TOOL_EXECUTION_DURATION = Histogram(
    "agent_tool_execution_duration_seconds", "Tool execution duration in seconds", labelnames=["tool_name", "status"]
)

# 工具语义校验失败计数，覆盖 bash_exec 容器契约和 aCLI catalog 校验。
AGENT_TOOL_SEMANTIC_VALIDATION_TOTAL = Counter(
    "agent_tool_semantic_validation_total",
    "Total number of semantic validation results before tool execution",
    labelnames=["tool_name", "validation_code"],
)

# ─── 2. 幻觉检测统计 ───────────────────────────────────────────────────────

# 幻觉检测总数（按幻觉类型分类）
AGENT_HALLUCINATION_DETECTED_TOTAL = Counter(
    "agent_hallucination_detected_total",
    "Total number of hallucinations detected in Agent output",
    labelnames=["hallucination_type"],
)

# 无证据结论计数（T4-2: agent_unsupported_claim_rate）
AGENT_UNSUPPORTED_CLAIM_TOTAL = Counter(
    "agent_unsupported_claim_total",
    "Total number of unsupported claims detected in Agent final report",
    labelnames=["claim_type"],
)

# ─── 3. 结构化输出校验统计 ─────────────────────────────────────────────────

# Schema 校验总数（按 schema 名称和状态分类）
AGENT_SCHEMA_VALIDATION_TOTAL = Counter(
    "agent_schema_validation_total",
    "Total number of schema validation checks on Agent output",
    labelnames=["schema_name", "status"],
)

# ─── 4. 验证优先闭环拦截统计 ───────────────────────────────────────────────

# 验证节点拦截计数
AGENT_VERIFICATION_BLOCKED_TOTAL = Counter(
    "agent_verification_blocked_total", "Total number of premature agent closures blocked by verification gate"
)

# ─── 5. 信息置信度统计 ─────────────────────────────────────────────────────

# 推理假设置信度（实时 Gauge）
AGENT_REASONING_CONFIDENCE = Gauge("agent_reasoning_confidence", "LLM reasoning hypothesis confidence score")

# 平均信息置信度（T4-2: agent_information_confidence_avg）
AGENT_INFORMATION_CONFIDENCE_SUM = Counter(
    "agent_information_confidence_sum",
    "Sum of information packet confidence values (for average calculation)",
    labelnames=["fact_type", "source"],
)

AGENT_INFORMATION_PACKET_COUNT = Counter(
    "agent_information_packet_count",
    "Count of information packets (for average confidence calculation)",
    labelnames=["fact_type", "source"],
)

# ─── 6. 解决效率统计 ───────────────────────────────────────────────────────

# 推理步数计数（T4-2: agent_mean_steps_to_resolution）
AGENT_REASONING_STEPS_TOTAL = Counter(
    "agent_reasoning_steps_total",
    "Total reasoning steps taken to reach diagnosis conclusion",
    labelnames=["session_id", "case_id"],
)

# 工单解决步数（用于计算平均步数）
AGENT_RESOLUTION_STEPS = Histogram(
    "agent_resolution_steps", "Number of steps taken to resolve a case", buckets=[5, 10, 15, 20, 30, 50, 100]
)
