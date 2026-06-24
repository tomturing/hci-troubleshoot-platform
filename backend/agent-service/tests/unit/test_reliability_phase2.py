"""
阶段二：轻量事实体系 — 单元测试

覆盖：
  - InformationPacket 序列化 / 置信度标签 / 新鲜度标签
  - StaleDataGuard.is_stale() 各阈值边界
  - EvidenceBundle.add_packet() / to_prompt_section() / has_sufficient_facts()
  - FactStore 读写（Redis Mock）/ 冲突检测 / 索引构建
  - EvidenceBuilder.build_for_intent_classification() 合并逻辑
  - EvidenceBuilder.check_information_quality() 质量评分与澄清触发
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from shared.models.information import EvidenceBundle, FactSource, InformationPacket, StaleDataGuard

# ─── InformationPacket 测试 ─────────────────────────────────────────────────


class TestInformationPacket:
    """测试 InformationPacket 数据结构"""

    def test_default_freshness_ts(self):
        """默认采集时间戳为当前时间"""
        before = time.time()
        pkt = InformationPacket(key="vm_name", value="vm-01", source=FactSource.ENV_INJECT)
        after = time.time()
        assert before <= pkt.freshness_ts <= after

    def test_age_seconds(self):
        """age_seconds 返回正确的秒数"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.TOOL_EXEC, freshness_ts=time.time() - 100)
        assert 99 < pkt.age_seconds() < 102

    def test_confidence_label_high(self):
        """置信度 >= 0.9 标注为「高」"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.TOOL_EXEC, confidence=0.95)
        assert pkt._confidence_label() == "高"

    def test_confidence_label_mid(self):
        """置信度 >= 0.7 标注为「中」"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.ENV_INJECT, confidence=0.80)
        assert pkt._confidence_label() == "中"

    def test_confidence_label_low(self):
        """置信度 < 0.7 标注为「低」"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.LLM_INFERENCE, confidence=0.50)
        assert pkt._confidence_label() == "低"

    def test_freshness_label_seconds(self):
        """60 秒内的新鲜度标签包含「秒前」"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.TOOL_EXEC, freshness_ts=time.time() - 30)
        label = pkt._freshness_label()
        assert "秒前" in label

    def test_freshness_label_minutes(self):
        """60~3600 秒的新鲜度标签包含「分钟前」"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.TOOL_EXEC, freshness_ts=time.time() - 180)
        label = pkt._freshness_label()
        assert "分钟前" in label

    def test_freshness_label_hours(self):
        """超过 3600 秒的新鲜度标签包含「小时前」"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.TOOL_EXEC, freshness_ts=time.time() - 7200)
        label = pkt._freshness_label()
        assert "小时前" in label

    def test_to_prompt_dict_conflict(self):
        """冲突包含 warning 字段"""
        pkt = InformationPacket(key="x", value="a", source=FactSource.ENV_INJECT, conflict=True)
        d = pkt.to_prompt_dict()
        assert "warning" in d
        assert "冲突" in d["warning"]

    def test_to_prompt_dict_llm_inference_note(self):
        """LLM 推理且未验证时包含 note 字段"""
        pkt = InformationPacket(key="x", value="a", source=FactSource.LLM_INFERENCE, verified=False)
        d = pkt.to_prompt_dict()
        assert "note" in d
        assert "待验证" in d["note"]

    def test_to_prompt_dict_verified_llm_no_note(self):
        """LLM 推理已验证时不包含 note 字段"""
        pkt = InformationPacket(key="x", value="a", source=FactSource.LLM_INFERENCE, verified=True)
        d = pkt.to_prompt_dict()
        assert "note" not in d


# ─── StaleDataGuard 测试 ──────────────────────────────────────────────────


class TestStaleDataGuard:
    """测试 StaleDataGuard 过期阈值守卫"""

    def test_fresh_packet_not_stale(self):
        """刚采集的 packet 不过期"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.TOOL_EXEC)
        assert StaleDataGuard.is_stale(pkt, "task_status") is False

    def test_task_status_stale_after_60s(self):
        """task_status 60 秒后过期"""
        pkt = InformationPacket(
            key="task_status", value="ok", source=FactSource.ENV_INJECT, freshness_ts=time.time() - 61
        )
        assert StaleDataGuard.is_stale(pkt, "task_status") is True

    def test_task_status_fresh_at_59s(self):
        """task_status 59 秒时不过期"""
        pkt = InformationPacket(
            key="task_status", value="ok", source=FactSource.ENV_INJECT, freshness_ts=time.time() - 59
        )
        assert StaleDataGuard.is_stale(pkt, "task_status") is False

    def test_vm_status_stale_after_30s(self):
        """vm_status 30 秒后过期（T2-1：阈值从 180s 改为 30s）"""
        pkt = InformationPacket(
            key="vm_status", value="running", source=FactSource.ENV_INJECT, freshness_ts=time.time() - 31
        )
        assert StaleDataGuard.is_stale(pkt, "vm_status") is True

    def test_vm_status_fresh_at_29s(self):
        """vm_status 29 秒不过期"""
        pkt = InformationPacket(
            key="vm_status", value="running", source=FactSource.ENV_INJECT, freshness_ts=time.time() - 29
        )
        assert StaleDataGuard.is_stale(pkt, "vm_status") is False

    def test_disk_health_stale_after_600s(self):
        """disk_health 600 秒后过期"""
        pkt = InformationPacket(
            key="disk_health", value="OK", source=FactSource.TOOL_EXEC, freshness_ts=time.time() - 601
        )
        assert StaleDataGuard.is_stale(pkt, "disk_health") is True

    def test_case_description_never_stale(self):
        """case_description 类型永不过期"""
        pkt = InformationPacket(
            key="case_description",
            value="磁盘故障",
            source=FactSource.USER_INPUT,
            freshness_ts=time.time() - 86400 * 30,
        )  # 30 天前
        assert StaleDataGuard.is_stale(pkt, "case_description") is False

    def test_user_input_never_stale(self):
        """user_input 类型永不过期"""
        pkt = InformationPacket(
            key="x", value="y", source=FactSource.USER_INPUT, freshness_ts=time.time() - 86400
        )  # 1 天前
        assert StaleDataGuard.is_stale(pkt, "user_input") is False

    def test_unknown_type_uses_default_300s(self):
        """未知类型使用默认 300 秒阈值"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.ENV_INJECT, freshness_ts=time.time() - 301)
        assert StaleDataGuard.is_stale(pkt, "unknown_type") is True

    def test_annotate_staleness_stale(self):
        """过期 packet 的注解包含「已过期」"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.ENV_INJECT, freshness_ts=time.time() - 400)
        annotation = StaleDataGuard.annotate_staleness(pkt, "default")
        assert "已过期" in annotation

    def test_annotate_staleness_fresh(self):
        """新鲜 packet 的注解包含「来源」"""
        pkt = InformationPacket(key="x", value=1, source=FactSource.TOOL_EXEC)
        annotation = StaleDataGuard.annotate_staleness(pkt, "task_status")
        assert "来源" in annotation
        assert "tool_exec" in annotation


# ─── EvidenceBundle 测试 ──────────────────────────────────────────────────


class TestEvidenceBundle:
    """测试 EvidenceBundle 事实集合"""

    def test_add_packet_increases_facts(self):
        """add_packet 添加事实"""
        bundle = EvidenceBundle(intent="intent_classification")
        pkt = InformationPacket(key="vm_name", value="vm-01", source=FactSource.ENV_INJECT)
        bundle.add_packet(pkt, fact_type="vm_status")
        assert len(bundle.facts) == 1

    def test_add_stale_packet_marks_stale(self):
        """过期的 packet 被记入 stale_keys"""
        bundle = EvidenceBundle(intent="intent_classification")
        pkt = InformationPacket(
            key="task_status", value="ok", source=FactSource.ENV_INJECT, freshness_ts=time.time() - 200
        )
        bundle.add_packet(pkt, fact_type="task_status")
        assert "task_status" in bundle.stale_keys

    def test_add_conflict_packet_marks_conflict(self):
        """冲突 packet 被记入 conflict_keys"""
        bundle = EvidenceBundle(intent="intent_classification")
        pkt = InformationPacket(key="vm_name", value="vm-01", source=FactSource.ENV_INJECT, conflict=True)
        bundle.add_packet(pkt, fact_type="vm_status")
        assert "vm_name" in bundle.conflict_keys

    def test_to_prompt_section_empty(self):
        """空 Bundle 返回提示采集命令的文本"""
        bundle = EvidenceBundle(intent="intent_classification")
        section = bundle.to_prompt_section()
        assert "暂无" in section

    def test_to_prompt_section_with_facts(self):
        """非空 Bundle 的 section 包含事实键名"""
        bundle = EvidenceBundle(intent="intent_classification")
        pkt = InformationPacket(key="vm_name", value="vm-prod-01", source=FactSource.ENV_INJECT)
        bundle.add_packet(pkt, fact_type="vm_status")
        section = bundle.to_prompt_section()
        assert "vm_name" in section
        assert "vm-prod-01" in section

    def test_to_prompt_section_stale_warning(self):
        """过期字段的 section 包含过期警告"""
        bundle = EvidenceBundle(intent="intent_classification")
        pkt = InformationPacket(
            key="task_status", value="running", source=FactSource.ENV_INJECT, freshness_ts=time.time() - 200
        )
        bundle.add_packet(pkt, fact_type="task_status")
        section = bundle.to_prompt_section()
        assert "过期" in section

    def test_has_sufficient_facts_empty(self):
        """空 Bundle 不满足最小事实要求"""
        bundle = EvidenceBundle(intent="intent_classification")
        assert bundle.has_sufficient_facts(min_count=1) is False

    def test_has_sufficient_facts_with_data(self):
        """有效 facts 满足最小事实要求"""
        bundle = EvidenceBundle(intent="intent_classification")
        pkt = InformationPacket(key="env_info", value="cluster-a", source=FactSource.ENV_INJECT)
        bundle.add_packet(pkt, fact_type="env_inject")
        assert bundle.has_sufficient_facts(min_count=1) is True

    def test_token_estimate_increases(self):
        """添加事实后 token 估算值增加"""
        bundle = EvidenceBundle(intent="intent_classification")
        before = bundle.total_tokens_estimate
        pkt = InformationPacket(key="env_info", value="a" * 100, source=FactSource.ENV_INJECT)
        bundle.add_packet(pkt)
        assert bundle.total_tokens_estimate > before


# ─── FactStore 测试（Mock Redis）──────────────────────────────────────────


class TestFactStore:
    """测试 FactStore Redis 读写和冲突检测"""

    def _make_store(self, with_db: bool = False) -> tuple:
        """创建带 Mock Redis 和可选 Mock PG 的 FactStore

        T2-3: PG-first 逻辑需要 mock db_session_factory
        """
        from app.services.fact_store import FactStore

        mock_redis = AsyncMock()
        mock_redis.lrange.return_value = []

        if with_db:
            mock_db_session = AsyncMock()
            mock_db_session.__aenter__.return_value = mock_db_session
            mock_result = MagicMock()
            mock_result.scalar.return_value = None
            mock_result.scalars.return_value.all.return_value = []
            mock_db_session.execute.return_value = mock_result
            mock_db_session.add = MagicMock()
            mock_session_factory = MagicMock()
            mock_session_factory.return_value = mock_db_session
            store = FactStore(redis=mock_redis, db_session_factory=mock_session_factory)
            return store, mock_redis, mock_db_session
        else:
            store = FactStore(redis=mock_redis)
            return store, mock_redis, None

    @pytest.mark.asyncio
    async def test_write_success(self):
        """write() 成功写入且调用 redis.set"""
        store, mock_redis, _ = self._make_store(with_db=False)
        mock_redis.get.return_value = None  # 无旧值
        mock_redis.lrange.return_value = []

        pkt = InformationPacket(key="vm_name", value="vm-01", source=FactSource.ENV_INJECT)
        result = await store.write("sess-001", pkt, fact_type="vm_status")

        assert result is True
        mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_conflict_detection(self):
        """同一 key 不同值时检测冲突并置 conflict=True

        T2-3: 冲突检测基于 PG 权威值，需要 mock db_session_factory 返回旧值
        """
        store, mock_redis, mock_db_session = self._make_store(with_db=True)
        # 模拟 PG 中已有旧值（用于冲突检测）
        mock_result = MagicMock()
        mock_result.scalar.return_value = "vm-01"  # PG 返回旧值
        mock_db_session.execute.return_value = mock_result

        pkt = InformationPacket(key="vm_name", value="vm-02", source=FactSource.TOOL_EXEC)
        await store.write("sess-001", pkt, fact_type="vm_status")

        # 冲突应被标记
        assert pkt.conflict is True

    @pytest.mark.asyncio
    async def test_write_redis_error_graceful(self):
        """Redis 不可用但 PG 可用时 write() 成功写入 PG。

        T2-3: PG-first 逻辑，Redis 错误不阻塞主流程。
        """
        store, mock_redis, mock_db_session = self._make_store(with_db=True)
        mock_redis.set.side_effect = Exception("Redis connection refused")

        pkt = InformationPacket(key="x", value=1, source=FactSource.ENV_INJECT)
        result = await store.write("sess-001", pkt, fact_type="default")
        # PG 写入成功，Redis 错误不影响返回值
        assert result is True

    @pytest.mark.asyncio
    async def test_read_not_found(self):
        """read() key 不存在时返回 None"""
        store, mock_redis, _ = self._make_store(with_db=False)
        mock_redis.get.return_value = None

        result = await store.read("sess-001", "vm_status", "vm_name")
        assert result is None

    @pytest.mark.asyncio
    async def test_read_deserialize(self):
        """read() 正确反序列化存储的 JSON"""
        import json

        store, mock_redis, _ = self._make_store(with_db=False)
        data = {
            "key": "vm_name",
            "value": "vm-prod-01",
            "source": "env_inject",
            "freshness_ts": time.time(),
            "confidence": 0.9,
            "raw_evidence": None,
            "verified": False,
            "conflict": False,
            "tags": [],
        }
        mock_redis.get.return_value = json.dumps(data).encode()

        result = await store.read("sess-001", "vm_status", "vm_name")
        assert result is not None
        assert result.key == "vm_name"
        assert result.value == "vm-prod-01"
        assert result.source == FactSource.ENV_INJECT

    @pytest.mark.asyncio
    async def test_write_many_returns_count(self):
        """write_many() 返回成功写入的数量"""
        store, mock_redis, _ = self._make_store(with_db=False)
        mock_redis.get.return_value = None
        mock_redis.lrange.return_value = []

        packets = [InformationPacket(key=f"key_{i}", value=i, source=FactSource.ENV_INJECT) for i in range(3)]
        count = await store.write_many("sess-001", packets, fact_type="default")
        assert count == 3


# ─── EvidenceBuilder 测试 ────────────────────────────────────────────────


class TestEvidenceBuilder:
    """测试 EvidenceBuilder 事实构建和质量检查"""

    def _make_builder(self, stored_packets: list[InformationPacket] | None = None):
        """创建带 Mock FactStore 的 EvidenceBuilder"""
        from app.services.evidence_builder import EvidenceBuilder

        mock_store = AsyncMock()
        mock_store.read_all_types.return_value = stored_packets or []
        mock_store.read_all.return_value = stored_packets or []
        builder = EvidenceBuilder(fact_store=mock_store)
        return builder

    @pytest.mark.asyncio
    async def test_build_for_intent_classification_empty_env(self):
        """env_context 为空时返回空 Bundle（不崩溃）"""
        builder = self._make_builder()
        bundle = await builder.build_for_intent_classification("sess-001", env_context=None)
        assert bundle.intent == "intent_classification"

    @pytest.mark.asyncio
    async def test_build_from_env_context_plain(self):
        """普通 env_context 被转换为 InformationPacket"""
        builder = self._make_builder()
        env_context = {"vm_name": "vm-prod-01", "host_id": "host-a"}
        bundle = await builder.build_for_intent_classification("sess-001", env_context=env_context)
        assert len(bundle.facts) >= 2
        keys = [f["key"] for f in bundle.facts]
        assert "vm_name" in keys
        assert "host_id" in keys

    @pytest.mark.asyncio
    async def test_build_from_env_context_raw(self):
        """is_raw=True 的 env_context 被正确解析"""
        builder = self._make_builder()
        env_context = {
            "is_raw": True,
            "env_info": {"vm_name": "vm-01", "host": "h-01"},
            "alert_logs": [{"alert_id": 1}],
            "task_logs": [],
        }
        bundle = await builder.build_for_intent_classification("sess-001", env_context=env_context)
        keys = [f["key"] for f in bundle.facts]
        # env_info 内的 key 应该被提取出来
        assert "vm_name" in keys or "env_info" in keys

    @pytest.mark.asyncio
    async def test_stored_facts_merged_with_env_context(self):
        """FactStore 中的事实与 env_context 合并（FactStore 字段不重复）"""
        stored = [InformationPacket(key="vm_name", value="vm-stored", source=FactSource.TOOL_EXEC)]
        builder = self._make_builder(stored_packets=stored)
        env_context = {"host_id": "host-new"}  # 不同 key
        bundle = await builder.build_for_intent_classification("sess-001", env_context=env_context)
        keys = [f["key"] for f in bundle.facts]
        assert "vm_name" in keys
        assert "host_id" in keys

    @pytest.mark.asyncio
    async def test_check_quality_empty_env(self):
        """env_context 为空时质量评分为 0 且需要澄清"""
        builder = self._make_builder()
        report = await builder.check_information_quality("sess-001", env_context=None)
        assert report.quality_score == 0.0
        assert report.needs_clarification is True
        assert report.clarification_reason != ""

    @pytest.mark.asyncio
    async def test_check_quality_complete_env(self):
        """完整 env_context 质量评分较高，不触发澄清"""
        builder = self._make_builder()
        env_context = {
            "env_info": "cluster-a, host-01, 32GB RAM",
            "alert_logs": [{"id": 1, "msg": "disk I/O high"}],
            "task_logs": [{"id": 2, "status": "failed"}],
        }
        report = await builder.check_information_quality("sess-001", env_context=env_context)
        assert report.missing_keys == []
        assert report.needs_clarification is False

    @pytest.mark.asyncio
    async def test_check_quality_missing_keys(self):
        """缺少必填字段时 missing_keys 非空且触发澄清"""
        builder = self._make_builder()
        env_context = {"env_info": "something"}  # 缺 alert_logs 和 task_logs
        report = await builder.check_information_quality("sess-001", env_context=env_context)
        assert len(report.missing_keys) >= 1
        assert report.needs_clarification is True


# ─── env_context 转 InformationPacket 兼容性测试 ─────────────────────────


class TestEnvContextToPackets:
    """测试 env_context → InformationPacket 的向后兼容转换"""

    def _call(self, env_context: dict) -> list[InformationPacket]:
        from app.services.evidence_builder import EvidenceBuilder

        return EvidenceBuilder._env_context_to_packets(env_context)

    def test_empty_dict(self):
        """空字典返回空列表"""
        result = self._call({})
        assert result == []

    def test_plain_dict_converts(self):
        """普通 k-v 字典转为同等数量的 InformationPacket"""
        result = self._call({"vm_name": "vm-01", "host_id": "host-a"})
        assert len(result) == 2
        keys = {p.key for p in result}
        assert "vm_name" in keys
        assert "host_id" in keys

    def test_plain_dict_skips_empty_values(self):
        """空值字段被跳过"""
        result = self._call({"vm_name": "vm-01", "empty_key": "", "none_key": None})
        keys = {p.key for p in result}
        assert "empty_key" not in keys
        assert "none_key" not in keys

    def test_raw_format_extracts_env_info_dict(self):
        """is_raw=True + env_info 为字典时，展开每个 key 为独立 packet"""
        result = self._call(
            {
                "is_raw": True,
                "env_info": {"vm_name": "vm-01", "cpu_cores": 8},
                "alert_logs": [],
                "task_logs": [],
            }
        )
        keys = {p.key for p in result}
        assert "vm_name" in keys
        assert "cpu_cores" in keys

    def test_raw_format_extracts_alert_logs(self):
        """is_raw=True + alert_logs 非空时，生成 alert_logs packet"""
        result = self._call(
            {
                "is_raw": True,
                "env_info": {},
                "alert_logs": [{"id": 1, "msg": "disk fail"}],
                "task_logs": [],
            }
        )
        keys = {p.key for p in result}
        assert "alert_logs" in keys

    def test_source_is_env_inject(self):
        """所有转换 packet 来源均为 env_inject"""
        result = self._call({"vm_name": "vm-01"})
        for p in result:
            assert p.source == FactSource.ENV_INJECT


# ─── InvestigationAgent 信息质量检查测试 ──────────────────────────────────────────


class TestInvestigationAgentQualityCheck:
    """测试 InvestigationAgent 在 process() 诊断开始时的信息质量检查拦截"""

    @pytest.mark.asyncio
    async def test_quality_check_passes_and_continues(self):
        """当环境上下文完整时，信息质量检查通过，不拦截并正常继续"""
        from app.adapters.agents.htp.investigation_agent import InvestigationAgent
        from app.domain.agent_port import AgentInteractiveRequest

        kb = MagicMock()
        kb.route_by_category = AsyncMock(return_value={"track": "kbd", "results": []})
        kb.search_cases_with_steps = AsyncMock(return_value=[])

        registry = MagicMock()
        mock_client = MagicMock()

        async def fake_stream(*args, **kwargs):
            yield "诊断报告块"

        mock_client.chat_completion_stream = fake_stream
        registry.get_client.return_value = mock_client

        agent = InvestigationAgent(
            ai_registry=registry,
            kb_client=kb,
            tool_executor=MagicMock(),
        )

        env_context = {
            "env_info": "cluster-a",
            "alert_logs": [{"id": 1, "msg": "high load"}],
            "task_logs": [{"id": 2, "status": "failed"}],
        }

        events = []
        async for event in agent.process(
            session_id="sess-q-01",
            messages=[{"role": "user", "content": "vm failure"}],
            category_id="虚拟机-003",
            diagnostic_stage="S1",
            env_context=env_context,
            assistant_type="htp-agent",
        ):
            events.append(event)

        # 检查是否正常继续，没有 yield clarifying interactive request
        interactive_requests = [e for e in events if isinstance(e, AgentInteractiveRequest)]
        assert len(interactive_requests) == 0
        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_quality_check_fails_and_yields_clarification(self):
        """当环境上下文缺失关键字段时，触发 clarification 交互拦截并提前返回"""
        from unittest.mock import AsyncMock

        from app.adapters.agents.htp.investigation_agent import InvestigationAgent
        from app.domain.agent_port import AgentInteractiveRequest

        kb = MagicMock()
        # 质量检查现在在 SOP 路由之后，需要 route_by_category 返回非 SOP 结果
        kb.route_by_category = AsyncMock(return_value={"track": "kbd", "results": []})
        agent = InvestigationAgent(
            ai_registry=MagicMock(),
            kb_client=kb,
            tool_executor=MagicMock(),
        )

        # 空环境上下文
        env_context = {}

        events = []
        async for event in agent.process(
            session_id="sess-q-02",
            messages=[{"role": "user", "content": "vm failure"}],
            category_id="虚拟机-003",
            diagnostic_stage="S1",
            env_context=env_context,
            assistant_type="htp-agent",
        ):
            events.append(event)

        # 非 SOP 命中时仍应触发信息质量澄清
        interactive_requests = [e for e in events if isinstance(e, AgentInteractiveRequest)]
        assert len(interactive_requests) == 1
        req = interactive_requests[0]
        assert req.kind == "information_clarification"
        assert "clarify-" in req.request_id
        assert len(req.options) > 0

    @pytest.mark.asyncio
    async def test_event_stream_with_interactive_request_no_error(self, mocker):
        """测试 _event_stream 当收到 AgentInteractiveRequest 时，不触发空流错误"""
        import app.routes.agent as agent_routes
        from app.domain.agent_port import AgentInteractiveRequest
        from app.routes.agent import AgentStreamRequest, _event_stream

        mock_router = MagicMock()

        async def fake_process(*args, **kwargs):
            yield AgentInteractiveRequest(
                request_id="test-req",
                acp_session_id="sess-01",
                kind="test-kind",
                title="test-title",
                prompt="test-prompt",
                options=[],
            )

        mock_router.process = fake_process
        original_router = agent_routes._agent_router
        agent_routes._agent_router = mock_router

        try:
            req = AgentStreamRequest(
                session_id="sess-01",
                case_id="case-01",
                user_id="user-01",
                assistant_type="htp-agent",
                messages=[{"role": "user", "content": "hello"}],
            )

            events = []
            async for sse_event in _event_stream(req):
                events.append(sse_event)

            # 校验输出的 SSE 事件中包含 interactive_request，但不包含 type="error"
            import json

            event_types = []
            for ev in events:
                if ev.startswith("data: "):
                    data = json.loads(ev[6:].strip())
                    event_types.append(data.get("type"))

            assert "interactive_request" in event_types
            assert "error" not in event_types
            assert "done" in event_types
        finally:
            agent_routes._agent_router = original_router
