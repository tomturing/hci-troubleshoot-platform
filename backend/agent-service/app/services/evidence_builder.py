"""
EvidenceBuilder — 按意图从 FactStore 检索并构建 EvidenceBundle

职责：
  - 为 S0 阶段构建 intent_classification 事实集（build_for_intent_classification）
  - 为 S3 阶段构建 hypothesis_verification 事实集
  - 为 S4 阶段构建 remediation 事实集
  - 提供信息质量检查（_check_information_quality），在置信度不足时生成澄清请求

设计：
  - EvidenceBuilder 消费 FactStore，是 FactStore 的高层封装
  - 支持 env_context 字典直接转换为 InformationPacket（向后兼容旧调用路径）
  - 所有方法均为纯函数或 async，无副作用存储
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.fact_store import FactStore
from shared.models.information import EvidenceBundle, FactSource, InformationPacket, StaleDataGuard

logger = logging.getLogger("evidence-builder")

# 信息质量置信度阈值，低于此值触发澄清请求
CONFIDENCE_THRESHOLD = 0.75

# S0 阶段环境上下文必填字段（缺失时降低 Bundle 质量评分）
S0_REQUIRED_KEYS = {"env_info", "alert_logs", "task_logs"}


class EvidenceBuilder:
    """从 FactStore 构建各阶段 EvidenceBundle 的高层服务。

    使用方式：
        builder = EvidenceBuilder(fact_store)
        bundle = await builder.build_for_intent_classification(session_id, env_context)
        prompt_section = bundle.to_prompt_section()
    """

    def __init__(self, fact_store: FactStore | None = None) -> None:
        """
        Args:
            fact_store: 可选。若提供，则优先从 Redis 读取已有事实；
                        若不提供，则只能基于传入的 env_context 构建（无持久化）。
        """
        self._fact_store = fact_store

    # ─── S0 意图识别阶段 ──────────────────────────────────────────────────────

    async def build_for_intent_classification(
        self,
        session_id: str,
        env_context: dict[str, Any] | None = None,
    ) -> EvidenceBundle:
        """为 S0 意图识别构建 EvidenceBundle。

        优先从 Redis FactStore 加载，然后用 env_context 补充/刷新缺失的字段。
        自动进行多来源冲突检测和新鲜度标注。

        Args:
            session_id:  会话 ID
            env_context: 原始环境字典（向后兼容旧调用路径）

        Returns:
            EvidenceBundle，调用 to_prompt_section() 可直接注入 S0 Prompt
        """
        bundle = EvidenceBundle(intent="intent_classification")

        # 1. 从 FactStore 加载已存储事实（如果可用）
        stored_packets: list[InformationPacket] = []
        if self._fact_store:
            stored_packets = await self._fact_store.read_all_types(
                session_id,
                fact_types=["vm_status", "host_status", "alert_status", "task_status", "env_inject"],
            )

        # 2. 将 env_context 转换为 InformationPacket（旧路径兼容）
        env_packets = self._env_context_to_packets(env_context or {})

        # T2-2: 将新采集的 env_context 写入 FactStore
        if self._fact_store and env_packets:
            for packet in env_packets:
                from app.services.fact_store import _KEY_TO_FACT_TYPE
                fact_type = _KEY_TO_FACT_TYPE.get(packet.key, "default")
                await self._fact_store.write(session_id, packet, fact_type=fact_type)

        # 3. 合并：FactStore 优先，env_context 补充缺失字段
        all_packets = self._merge_packets(stored_packets, env_packets)

        # 4. 添加到 Bundle（含新鲜度/冲突标注）
        for packet in all_packets:
            from app.services.fact_store import _KEY_TO_FACT_TYPE
            fact_type = _KEY_TO_FACT_TYPE.get(packet.key, "default")
            bundle.add_packet(packet, fact_type=fact_type)

        logger.info(
            "S0 EvidenceBundle 构建完成: session=%s, facts=%d, stale=%d, conflict=%d, ~tokens=%d",
            session_id,
            len(bundle.facts),
            len(bundle.stale_keys),
            len(bundle.conflict_keys),
            bundle.total_tokens_estimate,
        )
        return bundle

    # ─── S3 假设验证阶段 ──────────────────────────────────────────────────────

    async def build_for_hypothesis_verification(
        self,
        session_id: str,
    ) -> EvidenceBundle:
        """为 S3 假设验证构建 EvidenceBundle（从 FactStore 加载工具执行结果）。"""
        bundle = EvidenceBundle(intent="hypothesis_verification")

        if not self._fact_store:
            return bundle

        packets = await self._fact_store.read_all_types(
            session_id,
            fact_types=["tool_exec", "disk_health", "network_config", "service_status", "process_status"],
        )
        for packet in packets:
            from app.services.fact_store import _KEY_TO_FACT_TYPE
            fact_type = _KEY_TO_FACT_TYPE.get(packet.key, "default")
            bundle.add_packet(packet, fact_type=fact_type)

        return bundle

    # ─── 信息质量检查（T2-4）─────────────────────────────────────────────────

    async def check_information_quality(
        self,
        session_id: str,
        env_context: dict[str, Any] | None = None,
    ) -> InformationQualityReport:
        """检查会话信息质量，决定是否需要向用户发起澄清请求。

        检查维度：
          1. env_context 是否为空或关键字段缺失
          2. 关键事实的置信度是否低于阈值（CONFIDENCE_THRESHOLD = 0.75）
          3. 关键事实是否已过期

        Returns:
            InformationQualityReport，包含质量评分和建议澄清的字段列表
        """
        report = InformationQualityReport(session_id=session_id)

        # 检查 1：env_context 为空
        if not env_context:
            report.missing_keys.extend(list(S0_REQUIRED_KEYS))
            report.quality_score = 0.0
            report.needs_clarification = True
            report.clarification_reason = "环境数据为空，无法开始诊断推理"
            return report

        # 检查 2：必填字段缺失
        for required_key in S0_REQUIRED_KEYS:
            val = env_context.get(required_key)
            if not val or val in ("", "N/A", "暂无数据", [], {}):
                report.missing_keys.append(required_key)

        # 检查 3：从 FactStore 读取置信度
        low_confidence_keys: list[str] = []
        stale_keys: list[str] = []
        if self._fact_store:
            for fact_type in ["vm_status", "host_status", "alert_status"]:
                packets = await self._fact_store.read_all(session_id, fact_type)
                for packet in packets:
                    if packet.confidence < CONFIDENCE_THRESHOLD:
                        low_confidence_keys.append(packet.key)
                    from app.services.fact_store import _KEY_TO_FACT_TYPE
                    ft = _KEY_TO_FACT_TYPE.get(packet.key, "default")
                    if StaleDataGuard.is_stale(packet, ft):
                        stale_keys.append(packet.key)

        report.low_confidence_keys = low_confidence_keys
        report.stale_keys = stale_keys

        # 综合评分
        total_checks = len(S0_REQUIRED_KEYS) + len(low_confidence_keys) + len(stale_keys)
        issues = len(report.missing_keys) + len(low_confidence_keys) + len(stale_keys)
        if total_checks > 0:
            report.quality_score = max(0.0, 1.0 - issues / max(total_checks, 3))
        else:
            report.quality_score = 0.8  # 无 FactStore 时默认中等质量

        # 触发澄清请求的条件
        if report.missing_keys:
            report.needs_clarification = True
            missing_labels = [_KEY_LABELS.get(k, k) for k in report.missing_keys]
            report.clarification_reason = f"以下环境信息缺失，请确认或补充：{', '.join(missing_labels)}"
        elif report.quality_score < 0.5:
            report.needs_clarification = True
            report.clarification_reason = (
                f"环境数据质量偏低（评分 {report.quality_score:.0%}），"
                "部分关键信息置信度不足或已过期，是否重新采集？"
            )

        logger.info(
            "信息质量检查: session=%s, score=%.2f, missing=%s, stale=%s, low_conf=%s",
            session_id,
            report.quality_score,
            report.missing_keys,
            report.stale_keys,
            report.low_confidence_keys,
        )
        return report

    # ─── 内部工具方法 ─────────────────────────────────────────────────────────

    @staticmethod
    def _env_context_to_packets(env_context: dict[str, Any]) -> list[InformationPacket]:
        """将旧版 env_context 字典转换为 InformationPacket 列表（向后兼容）。

        支持两种格式：
          1. is_raw=True 格式（原始字典/JSON）：从 env_info、alert_logs、task_logs 提取
          2. 普通格式：直接遍历顶层 key-value
        """
        packets: list[InformationPacket] = []
        now = time.time()

        if not env_context:
            return packets

        if env_context.get("is_raw"):
            # is_raw 格式：env_info 是字典，alert_logs/task_logs 是列表
            raw_env_info = env_context.get("env_info", {})
            if isinstance(raw_env_info, dict):
                for k, v in raw_env_info.items():
                    packets.append(InformationPacket(
                        key=k,
                        value=v,
                        source=FactSource.ENV_INJECT,
                        freshness_ts=now,
                        confidence=0.9,
                        tags=["env_info"],
                    ))
            elif isinstance(raw_env_info, str) and raw_env_info:
                packets.append(InformationPacket(
                    key="env_info",
                    value=raw_env_info,
                    source=FactSource.ENV_INJECT,
                    freshness_ts=now,
                    confidence=0.9,
                ))

            alert_logs = env_context.get("alert_logs", [])
            if alert_logs:
                packets.append(InformationPacket(
                    key="alert_logs",
                    value=alert_logs,
                    source=FactSource.ENV_INJECT,
                    freshness_ts=now,
                    confidence=0.95,
                    tags=["alert_status"],
                ))

            task_logs = env_context.get("task_logs", [])
            if task_logs:
                packets.append(InformationPacket(
                    key="task_logs",
                    value=task_logs,
                    source=FactSource.ENV_INJECT,
                    freshness_ts=now,
                    confidence=0.95,
                    tags=["task_status"],
                ))
        else:
            # 普通格式：直接遍历顶层键值
            for k, v in env_context.items():
                if v not in (None, "", [], {}):
                    packets.append(InformationPacket(
                        key=k,
                        value=v,
                        source=FactSource.ENV_INJECT,
                        freshness_ts=now,
                        confidence=0.85,
                    ))

        return packets

    @staticmethod
    def _merge_packets(
        stored: list[InformationPacket],
        fresh: list[InformationPacket],
    ) -> list[InformationPacket]:
        """合并 FactStore 存储事实和 env_context 新鲜事实。

        策略：相同 key 时，新鲜 env_context 数据优先（更新），保留 stored 中不冲突的字段。
        """
        stored_keys = {p.key for p in stored}
        result = list(stored)

        for packet in fresh:
            if packet.key not in stored_keys:
                result.append(packet)
            # 若已存在同名 key，fresh 中的值覆盖（env_context 刚采集，更新鲜）

        return result


# ─── 信息质量报告 ────────────────────────────────────────────────────────────
@dataclass
class InformationQualityReport:
    """信息质量检查结果报告。"""

    session_id: str
    quality_score: float = 1.0          # [0.0, 1.0]，低于 0.5 触发澄清
    needs_clarification: bool = False   # True 时应向用户发起澄清请求
    clarification_reason: str = ""      # 向用户展示的澄清原因
    missing_keys: list[str] = field(default_factory=list)
    low_confidence_keys: list[str] = field(default_factory=list)
    stale_keys: list[str] = field(default_factory=list)

    def to_clarification_prompt(self) -> str:
        """生成向用户展示的澄清提示文本。"""
        return self.clarification_reason or "需要补充环境信息以进行准确诊断。"


# ─── Key 标签映射（用于用户友好展示）─────────────────────────────────────────

_KEY_LABELS: dict[str, str] = {
    "env_info": "环境基础信息",
    "alert_logs": "告警日志",
    "task_logs": "任务日志",
    "vm_name": "虚拟机名称",
    "vm_status": "虚拟机状态",
    "host_id": "主机 ID",
    "disk_health_status": "磁盘健康状态",
    "service_status": "服务状态",
}
