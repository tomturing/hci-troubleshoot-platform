"""
FactStore — 轻量事实存储（基于 PostgreSQL 与 Redis 双轨读写）(T4-3)

职责：
  - PostgreSQL 扮演持久化数据源（fact 与 claim_evidence_link 表）
  - Redis 扮演 5 分钟加速缓存，过期时间为 300 秒
  - 自动进行多来源冲突检测，同一 key 的值有冲突时置 conflict 为 True
  - 提供 claim_evidence_link 写入功能，关联断言与事实
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from shared.models.information import EvidenceBundle, FactSource, InformationPacket, StaleDataGuard
from shared.models.reliability import ClaimVerification
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("fact-store")

# Redis Key 前缀
_KEY_PREFIX = "fact"
_INDEX_SUFFIX = "_index"


class FactStore:
    """轻量事实存储服务（PostgreSQL 权威 + Redis 缓存）。

    T2-3/T4-3: PostgreSQL 为权威持久化源，Redis 仅做 5min TTL 热缓存。
    读取路径：PG 优先（权威）→ Redis read-through cache 回写。
    写入路径：PG 先写（权威）→ Redis 后写（缓存）。
    冲突检测：PG 权威值作为判定基准。
    """

    def __init__(self, redis: Redis | None = None, db_session_factory: Any = None) -> None:
        self._redis = redis
        self._db_session_factory = db_session_factory

    # ─── 辅助方法 ────────────────────────────────────────────────────────────

    async def _resolve_case_id(self, session_id: str, db_session: AsyncSession) -> str:
        """从 session_id (可能是 conversation_id) 解析 case_id。"""
        try:
            val = uuid.UUID(session_id)
            from shared.models.conversation import Conversation

            stmt = select(Conversation.case_id).where(Conversation.conversation_id == val)
            res = await db_session.execute(stmt)
            case_id = res.scalar()
            if case_id:
                return case_id
        except ValueError:
            pass
        return session_id[:20]

    async def _resolve_fact_db_id(self, case_id: str, ref: str, db_session: AsyncSession) -> str | None:
        """根据 fact_id (UUID) 或 fact_key (字符串) 解析真实的 fact.id。"""
        from shared.models.fact import Fact

        stmt = select(Fact.id).where(Fact.case_id == case_id, (Fact.id == ref) | (Fact.key == ref))
        res = await db_session.execute(stmt)
        return res.scalar()

    # ─── 写入接口 ────────────────────────────────────────────────────────────

    async def write(
        self,
        session_id: str,
        packet: InformationPacket,
        fact_type: str = "default",
    ) -> bool:
        """将 InformationPacket 写入 PostgreSQL（权威）和 Redis Cache。

        T2-3: 冲突检测基于 PG 权威值，Redis 仅作缓存不做判定基准。
        """
        try:
            key_str = self._build_key(session_id, fact_type, packet.key)
            existing_value = None

            # 1. T2-3: 从 PG（权威源）读取已有值做冲突检测
            if self._db_session_factory:
                try:
                    async with self._db_session_factory() as db_session:
                        case_id = await self._resolve_case_id(session_id, db_session)
                        from shared.models.fact import Fact

                        stmt = select(Fact.normalized_value).where(
                            Fact.case_id == case_id, Fact.fact_type == fact_type, Fact.key == packet.key
                        )
                        res = await db_session.execute(stmt)
                        existing_value = res.scalar()
                except Exception as exc:
                    logger.warning("FactStore PG 冲突检测读取失败: %s", exc)
                    return False

            # 2. 冲突判定（基于 PG 权威值）
            if existing_value is not None and existing_value != packet.value:
                packet.conflict = True
                logger.warning(
                    "事实冲突检测: key=%s, 旧值=%s, 新值=%s, 来源=%s",
                    packet.key,
                    existing_value,
                    packet.value,
                    packet.source.value,
                )

            # 3. 写入 PostgreSQL (权威持久化)
            pg_written = False
            if self._db_session_factory:
                try:
                    async with self._db_session_factory() as db_session:
                        case_id = await self._resolve_case_id(session_id, db_session)
                        from shared.models.fact import Fact

                        stmt = select(Fact).where(
                            Fact.case_id == case_id, Fact.fact_type == fact_type, Fact.key == packet.key
                        )
                        res = await db_session.execute(stmt)
                        db_fact = res.scalar()

                        collected_at_dt = (
                            datetime.fromtimestamp(packet.freshness_ts, UTC)
                            if packet.freshness_ts
                            else datetime.now(UTC)
                        )

                        if db_fact:
                            db_fact.source = packet.source.value
                            db_fact.raw_ref = str(packet.raw_evidence) if packet.raw_evidence is not None else None
                            db_fact.normalized_value = packet.value
                            db_fact.confidence = packet.confidence
                            db_fact.freshness = "stale" if StaleDataGuard.is_stale(packet, fact_type) else "current"
                            db_fact.conflict = packet.conflict
                            db_fact.collected_at = collected_at_dt
                        else:
                            db_fact = Fact(
                                case_id=case_id,
                                fact_type=fact_type,
                                key=packet.key,
                                source=packet.source.value,
                                raw_ref=str(packet.raw_evidence) if packet.raw_evidence is not None else None,
                                normalized_value=packet.value,
                                confidence=packet.confidence,
                                freshness="stale" if StaleDataGuard.is_stale(packet, fact_type) else "current",
                                conflict=packet.conflict,
                                collected_at=collected_at_dt,
                            )
                            db_session.add(db_fact)
                        await db_session.commit()
                        pg_written = True
                except Exception as exc:
                    logger.warning("FactStore PG 写入失败: %s", exc)
                    return False

            # 4. 写入 Redis Cache (5分钟 TTL 300秒)
            redis_written = False
            if self._redis and (pg_written or not self._db_session_factory):
                try:
                    payload = self._serialize(packet)
                    await self._redis.set(key_str, json.dumps(payload, ensure_ascii=False), ex=300)
                    index_key = self._build_index_key(session_id, fact_type)
                    await self._ensure_in_index(index_key, packet.key, 300)
                    redis_written = True
                except Exception as exc:
                    logger.warning("FactStore Redis 写入失败: %s", exc)

            if self._db_session_factory and not pg_written:
                return False
            if not self._db_session_factory and not redis_written:
                return False

            # T4-2: 记录信息置信度指标
            try:
                from app.services.metrics import AGENT_INFORMATION_CONFIDENCE_SUM, AGENT_INFORMATION_PACKET_COUNT

                AGENT_INFORMATION_CONFIDENCE_SUM.labels(fact_type=fact_type, source=packet.source.value).inc(
                    packet.confidence
                )
                AGENT_INFORMATION_PACKET_COUNT.labels(fact_type=fact_type, source=packet.source.value).inc()
            except Exception:
                pass  # 指标写入不阻塞主流程

            logger.debug("FactStore 写入成功: session=%s, type=%s, key=%s", session_id, fact_type, packet.key)
            return True

        except Exception as exc:
            logger.warning("FactStore 写入总入口失败: %s", exc)
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

    async def write_claim_verification(self, session_id: str, verification: ClaimVerification) -> None:
        """将 ClaimVerification 结果持久化写入 PostgreSQL 中的 claim_evidence_link 表"""
        if not self._db_session_factory:
            return

        try:
            async with self._db_session_factory() as db_session:
                case_id = await self._resolve_case_id(session_id, db_session)
                from shared.models.fact import ClaimEvidenceLink

                # 为该 session_id/case_id 清理旧的 evidence links
                stmt_del = delete(ClaimEvidenceLink).where(ClaimEvidenceLink.case_id == case_id)
                await db_session.execute(stmt_del)

                # 插入新的 links
                for claim in verification.claims:
                    # supporting facts
                    for ref in claim.supporting_fact_ids:
                        fact_db_id = await self._resolve_fact_db_id(case_id, ref, db_session)
                        if fact_db_id:
                            link = ClaimEvidenceLink(
                                case_id=case_id,
                                claim_id=claim.claim_id,
                                fact_id=fact_db_id,
                                relation="supporting",
                                confidence=1.0,
                            )
                            db_session.add(link)
                    # contradicting facts
                    for ref in claim.contradicting_fact_ids:
                        fact_db_id = await self._resolve_fact_db_id(case_id, ref, db_session)
                        if fact_db_id:
                            link = ClaimEvidenceLink(
                                case_id=case_id,
                                claim_id=claim.claim_id,
                                fact_id=fact_db_id,
                                relation="contradicting",
                                confidence=1.0,
                            )
                            db_session.add(link)
                await db_session.commit()
                logger.info("FactStore 成功写入 ClaimVerification 链接")
        except Exception as exc:
            logger.warning("FactStore 写入 ClaimVerification 失败: %s", exc)

    # ─── 读取接口 ────────────────────────────────────────────────────────────

    async def read(
        self,
        session_id: str,
        fact_type: str,
        key: str,
    ) -> InformationPacket | None:
        """读取指定 key 的 InformationPacket。

        T2-3: PG 权威读路径 — PostgreSQL 为权威源，Redis 为 read-through 缓存。
        PG 不可用时才 fallback 到 Redis 缓存。
        """
        redis_key = self._build_key(session_id, fact_type, key)

        # 1. T2-3: 优先从 PostgreSQL（权威源）读取
        if self._db_session_factory:
            try:
                async with self._db_session_factory() as db_session:
                    case_id = await self._resolve_case_id(session_id, db_session)
                    from shared.models.fact import Fact

                    stmt = select(Fact).where(Fact.case_id == case_id, Fact.fact_type == fact_type, Fact.key == key)
                    res = await db_session.execute(stmt)
                    db_fact = res.scalar()
                    if not db_fact:
                        return None
                    packet = InformationPacket(
                        key=db_fact.key,
                        value=db_fact.normalized_value,
                        source=FactSource(db_fact.source),
                        freshness_ts=db_fact.collected_at.timestamp() if db_fact.collected_at else 0.0,
                        confidence=float(db_fact.confidence),
                        raw_evidence=db_fact.raw_ref,
                        verified=not db_fact.conflict,
                        conflict=db_fact.conflict,
                        tags=[],
                    )
                    # 回写 Redis 缓存
                    if self._redis:
                        try:
                            payload = self._serialize(packet)
                            await self._redis.set(redis_key, json.dumps(payload, ensure_ascii=False), ex=300)
                            index_key = self._build_index_key(session_id, fact_type)
                            await self._ensure_in_index(index_key, key, 300)
                        except Exception as cache_err:
                            logger.warning("FactStore Redis 缓存回写失败: %s", cache_err)
                    return packet
            except Exception as exc:
                logger.warning("FactStore PG 读取失败，fallback 到 Redis: %s", exc)

        # 2. PG 不可用时，fallback 到 Redis 缓存。PG 可用但未命中时已在上方返回 None。
        if self._redis:
            try:
                raw = await self._redis.get(redis_key)
                if raw:
                    return self._deserialize(json.loads(raw))
            except Exception as exc:
                logger.warning("FactStore Redis fallback 读取失败: %s", exc)

        return None

    async def read_all(
        self,
        session_id: str,
        fact_type: str,
    ) -> list[InformationPacket]:
        """读取指定 fact_type 下的所有 InformationPacket。

        T2-3: PG 权威读路径 — PostgreSQL 优先，Redis fallback。
        """
        index_key = self._build_index_key(session_id, fact_type)

        # 1. T2-3: 优先从 PostgreSQL（权威源）读取
        if self._db_session_factory:
            try:
                async with self._db_session_factory() as db_session:
                    case_id = await self._resolve_case_id(session_id, db_session)
                    from shared.models.fact import Fact

                    stmt = select(Fact).where(Fact.case_id == case_id, Fact.fact_type == fact_type)
                    res = await db_session.execute(stmt)
                    db_facts = res.scalars().all()

                    packets = []
                    for db_fact in db_facts:
                        packet = InformationPacket(
                            key=db_fact.key,
                            value=db_fact.normalized_value,
                            source=FactSource(db_fact.source),
                            freshness_ts=db_fact.collected_at.timestamp() if db_fact.collected_at else 0.0,
                            confidence=float(db_fact.confidence),
                            raw_evidence=db_fact.raw_ref,
                            verified=not db_fact.conflict,
                            conflict=db_fact.conflict,
                            tags=[],
                        )
                        packets.append(packet)
                        # 回写 Redis 缓存
                        if self._redis:
                            try:
                                redis_key = self._build_key(session_id, fact_type, packet.key)
                                payload = self._serialize(packet)
                                await self._redis.set(redis_key, json.dumps(payload, ensure_ascii=False), ex=300)
                                await self._ensure_in_index(index_key, packet.key, 300)
                            except Exception as cache_err:
                                logger.warning("FactStore Redis 缓存回写失败: %s", cache_err)
                    return packets
            except Exception as exc:
                logger.warning("FactStore PG 批量读取失败，fallback 到 Redis: %s", exc)

        # 2. PG 不可用时，fallback 到 Redis 缓存
        if self._redis:
            try:
                keys_raw = await self._redis.lrange(index_key, 0, -1)
                if keys_raw:
                    packets = []
                    for k in keys_raw:
                        key_str = k.decode() if isinstance(k, bytes) else k
                        raw = await self._redis.get(self._build_key(session_id, fact_type, key_str))
                        if raw:
                            packets.append(self._deserialize(json.loads(raw)))
                    return packets
            except Exception as exc:
                logger.warning("FactStore Redis fallback 批量读取失败: %s", exc)

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
        """从 Fact Store 中为指定意图构建 EvidenceBundle。"""
        if fact_types is None:
            fact_types = _INTENT_FACT_TYPES.get(intent, ["default"])

        bundle = EvidenceBundle(intent=intent)
        packets = await self.read_all_types(session_id, fact_types)

        for packet in packets:
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
        existing = await self._redis.lrange(index_key, 0, -1)
        key_bytes = key.encode() if isinstance(key, str) else key
        if key_bytes not in existing and key not in [k.decode() if isinstance(k, bytes) else k for k in existing]:
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

_INTENT_FACT_TYPES: dict[str, list[str]] = {
    "intent_classification": [
        "case_description",
        "vm_status",
        "host_status",
        "alert_status",
        "task_status",
        "env_inject",
    ],
    "hypothesis_verification": [
        "tool_exec",
        "disk_health",
        "network_config",
        "service_status",
        "process_status",
    ],
    "remediation": [
        "tool_exec",
        "vm_status",
        "service_status",
        "storage_config",
        "user_input",
    ],
}

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
