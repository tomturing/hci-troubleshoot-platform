"""
Prometheus Metrics definition for Agent Reliability
Agent可靠性核心指标 (T4-2)
"""

from prometheus_client import Counter, Gauge

# 1. 工具调用统计与成功率
AGENT_TOOL_CALL_TOTAL = Counter(
    "agent_tool_call_total",
    "Total number of tool calls by the Agent",
    labelnames=["tool_name", "status"]
)

# 2. 幻觉检测统计
AGENT_HALLUCINATION_DETECTED_TOTAL = Counter(
    "agent_hallucination_detected_total",
    "Total number of hallucinations detected in Agent output",
    labelnames=["hallucination_type"]
)

# 3. 结构化输出校验统计
AGENT_SCHEMA_VALIDATION_TOTAL = Counter(
    "agent_schema_validation_total",
    "Total number of schema validation checks on Agent output",
    labelnames=["schema_name", "status"]
)

# 4. 验证优先闭环拦截统计
AGENT_VERIFICATION_BLOCKED_TOTAL = Counter(
    "agent_verification_blocked_total",
    "Total number of premature agent closures blocked by verification gate"
)

# 5. 推理置信度评分
AGENT_REASONING_CONFIDENCE = Gauge(
    "agent_reasoning_confidence",
    "LLM reasoning hypothesis confidence score"
)
