import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.fact_store import FactStore
from app.services.metrics import (
    AGENT_HALLUCINATION_DETECTED_TOTAL,
    AGENT_INFORMATION_CONFIDENCE_SUM,
    AGENT_INFORMATION_PACKET_COUNT,
    AGENT_SCHEMA_VALIDATION_TOTAL,
    AGENT_TOOL_CALL_TOTAL,
    AGENT_VERIFICATION_BLOCKED_TOTAL,
)
from shared.models.information import FactSource, InformationPacket
from shared.models.reliability import Claim, ClaimVerification

# ─── 1. FactStore PostgreSQL + Redis Tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_fact_store_postgres_write():
    # Mock database session and query result
    mock_db_session = AsyncMock()
    mock_db_session.__aenter__.return_value = mock_db_session
    mock_result = MagicMock()
    mock_result.scalar.return_value = None
    mock_db_session.execute.return_value = mock_result
    mock_db_session.add = MagicMock()

    mock_session_factory = MagicMock()
    mock_session_factory.return_value = mock_db_session

    mock_redis = AsyncMock()
    mock_redis.get.return_value = None

    store = FactStore(redis=mock_redis, db_session_factory=mock_session_factory)

    packet = InformationPacket(key="vm_status", value="running", source=FactSource.ENV_INJECT)

    res = await store.write("sess-1", packet, fact_type="vm_status")

    assert res is True
    # Assert database add and commit called
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_called_once()
    # Assert Redis cache set called
    mock_redis.set.assert_called_once()


@pytest.mark.asyncio
async def test_fact_store_postgres_read_cache_hit():
    """T2-3: PG-first 逻辑 — PG 返回数据后回写 Redis 缓存"""
    mock_db_session = AsyncMock()
    mock_db_session.__aenter__.return_value = mock_db_session
    mock_session_factory = MagicMock()
    mock_session_factory.return_value = mock_db_session

    # Mock PG 返回数据
    mock_db_fact = MagicMock()
    mock_db_fact.key = "vm_status"
    mock_db_fact.normalized_value = "running"
    mock_db_fact.source = "env_inject"
    mock_db_fact.confidence = 1.0
    mock_db_fact.raw_ref = None
    mock_db_fact.conflict = False
    mock_db_fact.collected_at = MagicMock()
    mock_db_fact.collected_at.timestamp.return_value = time.time()

    mock_result = MagicMock()
    mock_result.scalar.return_value = mock_db_fact
    mock_db_session.execute.return_value = mock_result

    mock_redis = AsyncMock()
    mock_redis.lrange.return_value = []

    store = FactStore(redis=mock_redis, db_session_factory=mock_session_factory)

    packet = await store.read("sess-1", "vm_status", "vm_status")

    assert packet is not None
    assert packet.value == "running"
    # PG 应被查询（PG-first）
    assert mock_db_session.execute.call_count >= 1
    # Redis 缓存应被回写
    assert mock_redis.set.call_count >= 1


@pytest.mark.asyncio
async def test_fact_store_postgres_read_cache_miss_db_hit():
    # Cache miss
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None

    # DB Hit
    mock_db_fact = MagicMock()
    mock_db_fact.key = "vm_status"
    mock_db_fact.normalized_value = "running"
    mock_db_fact.source = "env_inject"
    mock_db_fact.confidence = 1.0
    mock_db_fact.raw_ref = None
    mock_db_fact.conflict = False
    mock_db_fact.collected_at = MagicMock(timestamp=MagicMock(return_value=time.time()))

    mock_db_session = AsyncMock()
    mock_db_session.__aenter__.return_value = mock_db_session
    mock_result = MagicMock()
    mock_result.scalar.return_value = mock_db_fact
    mock_db_session.execute.return_value = mock_result

    mock_session_factory = MagicMock()
    mock_session_factory.return_value = mock_db_session

    store = FactStore(redis=mock_redis, db_session_factory=mock_session_factory)

    packet = await store.read("sess-1", "vm_status", "vm_status")

    assert packet is not None
    assert packet.value == "running"
    # Postgres should be queried
    assert mock_db_session.execute.call_count > 0
    # Redis should be updated with cache write-through
    mock_redis.set.assert_called_once()


@pytest.mark.asyncio
async def test_fact_store_postgres_read_db_miss_does_not_fallback_to_redis():
    """PG 权威读路径命中空结果时，不允许 Redis 旧缓存反向污染事实。"""
    mock_db_session = AsyncMock()
    mock_db_session.__aenter__.return_value = mock_db_session
    mock_result = MagicMock()
    mock_result.scalar.return_value = None
    mock_db_session.execute.return_value = mock_result

    mock_session_factory = MagicMock(return_value=mock_db_session)
    mock_redis = AsyncMock()
    mock_redis.get.return_value = b'{"key":"vm_status","value":"stale","source":"redis_cache"}'

    store = FactStore(redis=mock_redis, db_session_factory=mock_session_factory)

    packet = await store.read("sess-1", "vm_status", "vm_status")

    assert packet is None
    mock_redis.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_fact_store_postgres_write_failure_does_not_write_redis_only():
    """PG 权威写入失败时，不允许只写 Redis 并返回成功。"""
    mock_db_session = AsyncMock()
    mock_db_session.__aenter__.return_value = mock_db_session
    mock_db_session.execute.side_effect = RuntimeError("pg down")

    mock_session_factory = MagicMock(return_value=mock_db_session)
    mock_redis = AsyncMock()

    store = FactStore(redis=mock_redis, db_session_factory=mock_session_factory)
    packet = InformationPacket(key="vm_status", value="running", source=FactSource.ENV_INJECT)

    res = await store.write("sess-1", packet, fact_type="vm_status")

    assert res is False
    mock_redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_fact_store_write_claim_verification():
    mock_db_session = AsyncMock()
    mock_db_session.__aenter__.return_value = mock_db_session
    mock_result = MagicMock()
    # First call to execute() in _resolve_case_id: scalar returns "case-1"
    # Second, third, fourth calls to execute() in _resolve_fact_db_id: scalar returns "fact-uuid"
    mock_result.scalar.side_effect = ["case-1", "fact-uuid", "fact-uuid"]
    mock_db_session.execute.return_value = mock_result
    mock_db_session.add = MagicMock()

    mock_session_factory = MagicMock()
    mock_session_factory.return_value = mock_db_session

    store = FactStore(redis=None, db_session_factory=mock_session_factory)

    verification = ClaimVerification(
        claims=[
            Claim(
                claim_id="claim-1",
                claim_text="VM has disk error",
                status="supported",
                supporting_fact_ids=["disk_health_status"],
                contradicting_fact_ids=["vm_status"],
            )
        ]
    )

    await store.write_claim_verification("sess-1", verification)

    # Assert delete existing and add new links called
    assert mock_db_session.add.call_count == 2
    mock_db_session.commit.assert_called_once()


# ─── 2. Prometheus Metrics Tests ────────────────────────────────────────────


def test_prometheus_metrics_increment():
    # Verify we can record metrics without raising exceptions
    before_call_count = AGENT_TOOL_CALL_TOTAL.labels(tool_name="test_tool", status="success")._value.get()

    AGENT_TOOL_CALL_TOTAL.labels(tool_name="test_tool", status="success").inc()

    after_call_count = AGENT_TOOL_CALL_TOTAL.labels(tool_name="test_tool", status="success")._value.get()
    assert after_call_count == before_call_count + 1

    before_schema_count = AGENT_SCHEMA_VALIDATION_TOTAL.labels(schema_name="TestSchema", status="failed")._value.get()
    AGENT_SCHEMA_VALIDATION_TOTAL.labels(schema_name="TestSchema", status="failed").inc()
    after_schema_count = AGENT_SCHEMA_VALIDATION_TOTAL.labels(schema_name="TestSchema", status="failed")._value.get()
    assert after_schema_count == before_schema_count + 1

    before_hallucination_count = AGENT_HALLUCINATION_DETECTED_TOTAL.labels(
        hallucination_type="phantom_tool"
    )._value.get()
    AGENT_HALLUCINATION_DETECTED_TOTAL.labels(hallucination_type="phantom_tool").inc()
    after_hallucination_count = AGENT_HALLUCINATION_DETECTED_TOTAL.labels(
        hallucination_type="phantom_tool"
    )._value.get()
    assert after_hallucination_count == before_hallucination_count + 1

    before_blocked_count = AGENT_VERIFICATION_BLOCKED_TOTAL._value.get()
    AGENT_VERIFICATION_BLOCKED_TOTAL.inc()
    after_blocked_count = AGENT_VERIFICATION_BLOCKED_TOTAL._value.get()
    assert after_blocked_count == before_blocked_count + 1

    labels = {"fact_type": "tool_exec", "source": "tool_exec"}
    before_conf_sum = AGENT_INFORMATION_CONFIDENCE_SUM.labels(**labels)._value.get()
    before_packet_count = AGENT_INFORMATION_PACKET_COUNT.labels(**labels)._value.get()
    AGENT_INFORMATION_CONFIDENCE_SUM.labels(**labels).inc(0.9)
    AGENT_INFORMATION_PACKET_COUNT.labels(**labels).inc()
    assert AGENT_INFORMATION_CONFIDENCE_SUM.labels(**labels)._value.get() == before_conf_sum + 0.9
    assert AGENT_INFORMATION_PACKET_COUNT.labels(**labels)._value.get() == before_packet_count + 1
