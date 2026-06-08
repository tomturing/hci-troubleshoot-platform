"""
FactStore — 轻量事实存储（基于 Redis）

职责：
  - 将 InformationPacket 以 JSON 形式写入 Redis，TTL 按 StaleDataGuard 阈值
  - 按 fact_type 聚合读取，支持按 session_id 命名空间隔离
  - 多来源冲突检测：同一 key 已有不同值时追加 conflict 标记，不覆盖旧值

Redis Key 设计：
  fact:{session_id}:{fact_type}:{key}        — 单条事实（JSON）
  fact:{session_id}:{fact_type}:_index       — 该 fact_type 下的 key 列表（LIST）

安全边界：
  Redis 不可用时，FactStore 降级为无状态模式（不缓存），不抛出异常
"""

from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis
from shared.models.information import EvidenceBundle, FactSource, InformationPacket, StaleDataGuard

logger = logging.getLogger("fact-store")

# Redis Key 前缀
_KEY_PREFIX = "fact"
_INDEX_SUFFIX = "_index"


class FactStore:
    """轻量事实存储服务（Redis 实现）。

    设计为无状态单例友好型，所有方法均为 async。
    Redis 不可用时优雅降级，不阻断 Agent 推理流程。
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ─── 写入接口 ────────────────────────────────────────────────────────────

    async def write(
        self,
        session_id: str,
        packet: InformationPacket,
        fact_type: str = "default",
    ) -> bool:
        """将 InformationPacket 写入 Redis Fact Store。

        Args:
            session_id: 会话 ID（命名空间隔离）
            packet:     待写入的信息包
            fact_type:  事实类型（决定 TTL 阈值和 Key 分组）

        Returns:
            True 写入成功，False 降级（Redis 不可用）
        """
        try:
            key = self._build_key(session_id, fact_type, packet.key)
            index_key = self._build_index_key(session_id, fact_type)

            # 冲突检测：读取已有值
            existing_raw = await self._redis.get(key)
            if existing_raw:
                existing = json.loads(existing_raw)
                existing_value = existing.get("value")
                if existing_value != packet.value:
                    # 多来源冲突：保留旧值，为新包打冲突标记
                    packet.conflict = True
                    logger.warning(
                        "事实冲突检测: key=%s, 旧值=%s, 新值=%s, 来源=%s",
                        packet.key,
                        existing_value,
                        packet.value,
                        packet.source.value,
                    )

            # 序列化写入
            payload = self._serialize(packet)
            ttl = int(StaleDataGuard.get_threshold(fact_type))
            if ttl == 0 or ttl > 86400:  # 无穷大按 24h 存储
                ttl = 86400

            await self._redis.set(key, json.dumps(payload, ensure_ascii=False), ex=ttl)

            # 更新索引（去重）
            await self._ensure_in_index(index_key, packet.key, ttl)

            logger.debug("FactStore 写入: session=%s, type=%s, key=%s", session_id, fact_type, packet.key)
            return True

        except Exception as exc:
            logger.warning("FactStore 写入失败（降级）: %s", exc)
            return False

    async def write_many(
        self,
        session_id: str,
        packets: list[InformationPacket],
        fact_type: str = "default",
    ) -> int:
        """批量写入，返回成功写入的数量。"""
        success_count = 0
        for packet in packets:
            if await self.write(session_id, packet, fact_type):
                success_count += 1
        return success_count

    # ─── 读取接口 ────────────────────────────────────────────────────────────

    async def read(
        self,
        session_id: str,
        fact_type: str,
        key: str,
    ) -> InformationPacket | None:
        """读取指定 key 的 InformationPacket。未命中或过期返回 None。"""
        try:
            redis_key = self._build_key(session_id, fact_type, key)
            raw = await self._redis.get(redis_key)
            if not raw:
                return None
            return self._deserialize(json.loads(raw))
        except Exception as exc:
            logger.warning("FactStore 读取失败: %s", exc)
            return None

    async def read_all(
        self,
        session_id: str,
        fact_type: str,
    ) -> list[InformationPacket]:
        """读取指定 fact_type 下的所有 InformationPacket。"""
        try:
            index_key = self._build_index_key(session_id, fact_type)
            keys_raw = await self._redis.lrange(index_key, 0, -1)
            packets: list[InformationPacket] = []
            for k in keys_raw:
                packet = await self.read(session_id, fact_type, k.decode() if isinstance(k, bytes) else k)
                if packet:
                    packets.append(packet)
            return packets
        except Exception as exc:
            logger.warning("FactStore 批量读取失败: %s", exc)
            return []

    async def read_all_types(
        self,
        session_id: str,
        fact_types: list[str],
    ) -> list[InformationPacket]:
        """跨多个 fact_type 读取事实（用于 EvidenceBundle 构建）。"""
        result: list[InformationPacket] = []
        for fact_type in fact_types:
            packets = await self.read_all(session_id, fact_type)
            result.extend(packets)
        return result

    # ─── EvidenceBundle 构建 ────────────────────────────────────────────────

    async def build_evidence_bundle(
        self,
        session_id: str,
        intent: str,
        fact_types: list[str] | None = None,
    ) -> EvidenceBundle:
        """从 Fact Store 中为指定意图构建 EvidenceBundle。

        Args:
            session_id: 会话 ID
            intent:     意图标识（intent_classification / hypothesis_verification / remediation）
            fact_types: 要包含的事实类型列表，None 则使用该意图的默认集合

        Returns:
            EvidenceBundle，可直接调用 to_prompt_section() 注入 Prompt
        """
        if fact_types is None:
            fact_types = _INTENT_FACT_TYPES.get(intent, ["default"])

        bundle = EvidenceBundle(intent=intent)
        packets = await self.read_all_types(session_id, fact_types)

        for packet in packets:
            # 从 fact_types 中找对应的 fact_type（用于阈值判断）
            ft = _KEY_TO_FACT_TYPE.get(packet.key, "default")
            bundle.add_packet(packet, fact_type=ft)

        logger.debug(
            "EvidenceBundle 构建完成: session=%s, intent=%s, facts=%d, stale=%d, conflict=%d",
            session_id,
            intent,
            len(bundle.facts),
            len(bundle.stale_keys),
            len(bundle.conflict_keys),
        )
        return bundle

    # ─── 辅助方法 ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_key(session_id: str, fact_type: str, key: str) -> str:
        return f"{_KEY_PREFIX}:{session_id}:{fact_type}:{key}"

    @staticmethod
    def _build_index_key(session_id: str, fact_type: str) -> str:
        return f"{_KEY_PREFIX}:{session_id}:{fact_type}:{_INDEX_SUFFIX}"

    async def _ensure_in_index(self, index_key: str, key: str, ttl: int) -> None:
        """确保 key 在索引列表中（去重）。"""
        # 检查是否已在索引中
        existing = await self._redis.lrange(index_key, 0, -1)
        key_bytes = key.encode() if isinstance(key, str) else key
        if key_bytes not in existing and key not in [
            k.decode() if isinstance(k, bytes) else k for k in existing
        ]:
            await self._redis.rpush(index_key, key)
            await self._redis.expire(index_key, ttl)

    @staticmethod
    def _serialize(packet: InformationPacket) -> dict[str, Any]:
        """将 InformationPacket 序列化为 JSON 兼容字典。"""
        return {
            "key": packet.key,
            "value": packet.value,
            "source": packet.source.value,
            "freshness_ts": packet.freshness_ts,
            "confidence": packet.confidence,
            "raw_evidence": packet.raw_evidence,
            "verified": packet.verified,
            "conflict": packet.conflict,
            "tags": packet.tags,
        }

    @staticmethod
    def _deserialize(data: dict[str, Any]) -> InformationPacket:
        """将 JSON 字典反序列化为 InformationPacket。"""
        return InformationPacket(
            key=data["key"],
            value=data["value"],
            source=FactSource(data.get("source", "env_inject")),
            freshness_ts=data.get("freshness_ts", 0.0),
            confidence=data.get("confidence", 1.0),
            raw_evidence=data.get("raw_evidence"),
            verified=data.get("verified", False),
            conflict=data.get("conflict", False),
            tags=data.get("tags", []),
        )


# ─── 意图 → 事实类型映射（EvidenceBuilder 使用）────────────────────────────

# 不同意图阶段需要关注的事实类型
_INTENT_FACT_TYPES: dict[str, list[str]] = {
    "intent_classification": [
        "case_description",  # 工单描述（静态）
        "vm_status",         # VM 状态（中频）
        "host_status",       # 主机状态（中频）
        "alert_status",      # 告警状态（中频）
        "task_status",       # 任务状态（高频）
        "env_inject",        # 环境注入数据
    ],
    "hypothesis_verification": [
        "tool_exec",         # 工具执行结果（实测数据）
        "disk_health",       # 磁盘健康
        "network_config",    # 网络配置
        "service_status",    # 服务状态
        "process_status",    # 进程状态
    ],
    "remediation": [
        "tool_exec",
        "vm_status",
        "service_status",
        "storage_config",
        "user_input",        # 用户确认的操作参数
    ],
}

# key 名称 → fact_type 映射（用于 StaleDataGuard 阈值查询）
_KEY_TO_FACT_TYPE: dict[str, str] = {
    "vm_name": "vm_status",
    "vm_status": "vm_status",
    "vm_power_state": "vm_status",
    "host_id": "host_status",
    "host_status": "host_status",
    "alert_count": "alert_status",
    "alert_logs": "alert_status",
    "task_status": "task_status",
    "task_logs": "task_logs",
    "env_info": "env_inject",
    "case_description": "case_description",
    "disk_health_status": "disk_health",
    "network_config": "network_config",
    "service_status": "service_status",
    "process_status": "process_status",
}
