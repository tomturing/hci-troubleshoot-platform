"""
InformationPacket / EvidenceBundle — 轻量事实体系核心数据结构

设计原则（方案 C Evidence Plane 最小子集）：
  - 每条环境信息以 InformationPacket 形式封装，携带来源、置信度、新鲜度
  - StaleDataGuard 定义各类数据的过期阈值
  - EvidenceBundle 按意图组织事实集合，供 S0/S3/S4 阶段的 Prompt 消费

来源枚举（source 字段）：
  user_input    — 用户在对话中主动提供的信息
  tool_exec     — 工具执行输出（最高置信度）
  kb_search     — 知识库检索结果
  llm_inference — LLM 推理/总结（最低置信度，需标注"待验证"）
  env_inject    — 环境自动注入（工单创建时采集）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ─── 数据来源枚举 ─────────────────────────────────────────────────────────────

class FactSource(StrEnum):
    """事实来源类型枚举。置信度参考：tool_exec > env_inject > kb_search > user_input > llm_inference"""

    USER_INPUT = "user_input"       # 用户在对话中主动提供
    TOOL_EXEC = "tool_exec"         # 工具执行输出（命令运行结果）
    KB_SEARCH = "kb_search"         # 知识库检索结果
    LLM_INFERENCE = "llm_inference" # LLM 推理/总结（需标注"待验证"）
    ENV_INJECT = "env_inject"       # 工单创建时自动注入的环境数据


# ─── 信息包（InformationPacket） ─────────────────────────────────────────────

@dataclass
class InformationPacket:
    """单条事实的封装单元。

    每条采集到的环境信息（vm_name、alert_count、disk_state 等）均封装为一个
    InformationPacket，携带来源、时间戳和置信度，供 EvidenceBuilder 检索和过滤。

    Args:
        key:           事实键名（如 "vm_name"、"disk_health_status"）
        value:         事实值，可为任意类型
        source:        来源类型（见 FactSource 枚举）
        freshness_ts:  采集时间戳（Unix 秒），默认为当前时间
        confidence:    置信度 [0.0, 1.0]，tool_exec 默认 1.0，llm_inference 默认 0.5
        raw_evidence:  原始证据字符串（工具 stdout、用户原文等），可选
        verified:      是否已被工具结果交叉验证
        conflict:      是否存在来源冲突（多来源值不一致时置为 True）
        tags:          附加标签（如 "network"、"storage"），供 EvidenceBundle 过滤
    """

    key: str
    value: Any
    source: FactSource
    freshness_ts: float = field(default_factory=time.time)
    confidence: float = 1.0
    raw_evidence: str | None = None
    verified: bool = False
    conflict: bool = False
    tags: list[str] = field(default_factory=list)

    def age_seconds(self) -> float:
        """返回该信息包的年龄（秒）。"""
        return time.time() - self.freshness_ts

    def to_prompt_dict(self) -> dict[str, Any]:
        """序列化为供 Prompt 注入的字典（仅保留关键字段）。"""
        freshness_label = self._freshness_label()
        confidence_label = self._confidence_label()
        result: dict[str, Any] = {
            "key": self.key,
            "value": self.value,
            "source": self.source.value,
            "freshness": freshness_label,
            "confidence": confidence_label,
        }
        if self.conflict:
            result["warning"] = "⚠️ 多来源冲突，值可能不准确"
        if not self.verified and self.source == FactSource.LLM_INFERENCE:
            result["note"] = "🔍 待验证（LLM 推理，非工具实测）"
        return result

    def _freshness_label(self) -> str:
        """将年龄转为人类可读标签。"""
        age = self.age_seconds()
        if age < 60:
            return f"采集于 {int(age)} 秒前"
        elif age < 3600:
            return f"采集于 {int(age / 60)} 分钟前"
        else:
            return f"采集于 {int(age / 3600)} 小时前（可能已过期）"

    def _confidence_label(self) -> str:
        """将置信度转为人类可读标签。"""
        if self.confidence >= 0.9:
            return "高"
        elif self.confidence >= 0.7:
            return "中"
        else:
            return "低"


# ─── 过期阈值守卫（StaleDataGuard） ──────────────────────────────────────────

class StaleDataGuard:
    """定义各类数据的过期阈值（秒），用于 is_stale() 判断。

    阈值设计原则：
      - 动态运行状态（进程、任务）变化快，阈值短（60s）
      - 虚拟机/主机状态较稳定，阈值中等（180s）
      - 存储/网络配置变化慢，阈值较长（600s）
      - 工单描述等静态数据无过期（inf）
    """

    # 各类型事实的过期阈值（单位：秒）
    THRESHOLDS: dict[str, float] = {
        # 高频变化类（任务队列、进程状态）
        "task_status": 60.0,
        "process_status": 60.0,
        "service_status": 60.0,
        # 中频变化类（VM 状态、主机状态）— T2-1：VM 阈值改为 30s（与清单一致）
        "vm_status": 30.0,
        "host_status": 180.0,
        "alert_status": 120.0,
        # 低频变化类（配置、存储、网络拓扑）
        "disk_health": 600.0,
        "network_config": 600.0,
        "storage_config": 600.0,
        # 静态数据（工单、用户信息）
        "case_description": float("inf"),
        "user_input": float("inf"),
        # 默认阈值（未知类型）
        "default": 300.0,
    }

    @classmethod
    def get_threshold(cls, fact_type: str) -> float:
        """获取指定类型的过期阈值，未知类型使用 default。"""
        return cls.THRESHOLDS.get(fact_type, cls.THRESHOLDS["default"])

    @classmethod
    def is_stale(cls, packet: InformationPacket, fact_type: str = "default") -> bool:
        """判断 InformationPacket 是否已过期。

        Args:
            packet:    待判断的信息包
            fact_type: 事实类型（用于查阈值），默认 "default"（300s）

        Returns:
            True 表示已过期，需重新采集
        """
        threshold = cls.get_threshold(fact_type)
        if threshold == float("inf"):
            return False
        return packet.age_seconds() > threshold

    @classmethod
    def annotate_staleness(cls, packet: InformationPacket, fact_type: str = "default") -> str:
        """返回新鲜度注解字符串，供 Prompt 注入使用。"""
        if cls.is_stale(packet, fact_type):
            return f"[已过期，建议重新采集，来源: {packet.source.value}]"
        return f"[{packet._freshness_label()}，来源: {packet.source.value}]"


# ─── 事实集合（EvidenceBundle） ───────────────────────────────────────────────

@dataclass
class EvidenceBundle:
    """按意图组织的事实集合，供 Agent 的 Prompt 消费。

    设计：EvidenceBundle 不直接持有 InformationPacket 对象，
    而是持有已经序列化为 Prompt 友好格式的字典列表，降低耦合。

    intent 字段标识用途：
      intent_classification  — S0 意图识别（需要工单描述 + 环境摘要）
      hypothesis_verification — S3 假设验证（需要工具执行结果 + 约束条件）
      remediation             — S4 修复阶段（需要验证过的事实 + 变量池）
    """

    intent: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    stale_keys: list[str] = field(default_factory=list)
    conflict_keys: list[str] = field(default_factory=list)
    total_tokens_estimate: int = 0

    def add_packet(
        self,
        packet: InformationPacket,
        fact_type: str = "default",
    ) -> None:
        """将 InformationPacket 添加到 Bundle（含新鲜度和冲突标注）。"""
        fact_dict = packet.to_prompt_dict()
        staleness = StaleDataGuard.annotate_staleness(packet, fact_type)
        fact_dict["staleness"] = staleness

        if StaleDataGuard.is_stale(packet, fact_type):
            self.stale_keys.append(packet.key)
        if packet.conflict:
            self.conflict_keys.append(packet.key)

        self.facts.append(fact_dict)
        # 粗估 token 数（1 中文 ≈ 1 token，1 英文词 ≈ 1 token）
        self.total_tokens_estimate += len(str(fact_dict)) // 3

    def to_prompt_section(self) -> str:
        """将 EvidenceBundle 序列化为 Prompt 中的「已知事实」章节文本。"""
        if not self.facts:
            return "【已知事实】\n（暂无采集到的环境事实，建议先执行环境采集命令）\n"

        lines: list[str] = ["【已知事实】（来源标注见括号）"]
        for fact in self.facts:
            key = fact.get("key", "")
            value = fact.get("value", "")
            staleness = fact.get("staleness", "")
            warning = fact.get("warning", "")
            note = fact.get("note", "")

            line = f"- {key}: {value} {staleness}"
            if warning:
                line += f"\n  {warning}"
            if note:
                line += f"\n  {note}"
            lines.append(line)

        # 追加整体质量摘要
        if self.stale_keys:
            lines.append(f"\n⚠️ 以下字段可能已过期，建议重新采集：{', '.join(self.stale_keys)}")
        if self.conflict_keys:
            lines.append(f"⚠️ 以下字段存在多来源冲突，请人工确认：{', '.join(self.conflict_keys)}")

        return "\n".join(lines)

    def has_sufficient_facts(self, min_count: int = 1) -> bool:
        """判断 Bundle 中是否有足够的事实用于推理。"""
        # 排除过期且低置信度的事实
        valid_facts = [
            f for f in self.facts
            if f.get("key") not in self.stale_keys or f.get("confidence") == "高"
        ]
        return len(valid_facts) >= min_count
