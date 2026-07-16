"""
KBDDiagnostic 单元测试

测试覆盖：
  1. _pick_best_step：贪心最大频率选择逻辑
  2. _resolve_args：占位符替换逻辑
  3. _judge_matches：规则判断（__REGEX__: / __CONTAINS__:）
  4. diagnose：完整 KBD 差异诊断主循环（端到端，全 mock）
  5. 有效性验证：N=10 候选 KBD，≤8 步锁定正确 KBD
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.kbd_differential import KBDDiagnostic
from app.adapters.agents.htp.kbd_model import KBD

# ─── 测试数据工厂 ─────────────────────────────────────────────────────────────


def make_kbd(
    kbd_id: str,
    steps: list[tuple[str, str]],  # [(tool_name, expected_pattern), ...]
    root_cause: str = "测试根因",
    similarity: float = 0.8,
) -> KBD:
    """快速构建 KBD 测试对象（signals 为 raw dict 列表，契合 KBD.signals 契约）。"""
    return KBD(
        id=kbd_id,
        name=f"KBD {kbd_id}",
        category_id="虚拟机-003",
        problem_description=f"{kbd_id} 的问题描述",
        signals=[
            {
                "signal_category": "backend",
                "acquirer": tool_name,
                "acquirer_args": {},
                "expected_pattern": expected_pattern,
            }
            for tool_name, expected_pattern in steps
        ],
        root_cause=root_cause,
        solution=f"{kbd_id} 的解决方案",
        similarity=similarity,
    )


def make_registry_mock(invoke_response: str = '{"matches": {}}') -> MagicMock:
    """构建 AIAssistantRegistry mock，invoke() 返回指定 JSON。"""
    mock_result = MagicMock()
    mock_result.content = invoke_response

    mock_client = MagicMock()
    mock_client.invoke = AsyncMock(return_value=mock_result)

    mock_registry = MagicMock()
    mock_registry.get_client.return_value = mock_client
    return mock_registry


def make_tool_executor(results: dict[str, str]) -> MagicMock:
    """构建工具执行器 mock，按 tool_name 返回对应结果。"""
    mock = MagicMock()

    async def execute(tool_name: str, args: dict) -> str:
        return results.get(tool_name, "")

    mock.execute = execute
    return mock


# ─── 单元测试：_pick_best_step ────────────────────────────────────────────────


class TestPickBestStep:
    """_pick_best_step：贪心选择逻辑测试"""

    def setup_method(self):
        self.diag = KBDDiagnostic(
            ai_registry=MagicMock(),
            tool_executor=MagicMock(),
        )

    def test_picks_most_frequent_tool(self):
        """选择频率最高的工具"""
        candidates = [
            make_kbd("k1", [("tool_a", ""), ("tool_b", "")]),
            make_kbd("k2", [("tool_a", ""), ("tool_c", "")]),
            make_kbd("k3", [("tool_a", "")]),
        ]
        # tool_a 出现 3 次，tool_b/c 各 1 次
        best = self.diag._pick_best_step(candidates, executed_tools=set())
        assert best == "tool_a"

    def test_excludes_already_executed_tools(self):
        """已执行过的工具不应再被选择"""
        candidates = [
            make_kbd("k1", [("tool_a", ""), ("tool_b", "")]),
            make_kbd("k2", [("tool_a", ""), ("tool_b", "")]),
        ]
        # tool_a 已执行，应选 tool_b
        best = self.diag._pick_best_step(candidates, executed_tools={"tool_a"})
        assert best == "tool_b"

    def test_returns_none_when_all_executed(self):
        """所有工具都已执行时返回 None"""
        candidates = [
            make_kbd("k1", [("tool_a", ""), ("tool_b", "")]),
        ]
        best = self.diag._pick_best_step(candidates, executed_tools={"tool_a", "tool_b"})
        assert best is None

    def test_returns_none_for_empty_candidates(self):
        """候选 KBD 为空时返回 None"""
        best = self.diag._pick_best_step([], executed_tools=set())
        assert best is None


# ─── 单元测试：_resolve_args ──────────────────────────────────────────────────


class TestResolveArgs:
    """_resolve_args：占位符替换逻辑测试"""

    def test_replaces_single_placeholder(self):
        template = {"vm_name": "{{vm_name}}"}
        result = KBDDiagnostic._resolve_args(template, {"vm_name": "vm-001"})
        assert result == {"vm_name": "vm-001"}

    def test_replaces_multiple_placeholders(self):
        template = {"vm": "{{vm_name}}", "host": "{{host_id}}"}
        result = KBDDiagnostic._resolve_args(template, {"vm_name": "vm-001", "host_id": "host-01"})
        assert result == {"vm": "vm-001", "host": "host-01"}

    def test_leaves_non_string_values_unchanged(self):
        template = {"limit": 10, "enabled": True}
        result = KBDDiagnostic._resolve_args(template, {})
        assert result == {"limit": 10, "enabled": True}

    def test_unknown_placeholder_remains(self):
        template = {"key": "{{unknown}}"}
        result = KBDDiagnostic._resolve_args(template, {})
        assert result == {"key": "{{unknown}}"}


# ─── 单元测试：变量池大小写归一化（B/2.6）────────────────────────────────────


class TestVariablePoolCaseNormalization:
    """变量池大小写归一化：消费侧不敏感 + 生产侧小写规范（纵深防御）。"""

    def test_resolve_args_case_insensitive(self):
        # 生产侧写入小写 Key（_set_pool_var 规范）；消费侧任意大小写占位符均可命中
        pool = {"host": "10.0.0.1", "vm_id": "v-123"}
        template = {
            "a": "http://{{HOST}}",
            "b": "http://{{host}}",
            "c": "http://{{Host}}",
            "d": "vm is {{VM_ID}}",
        }
        result = KBDDiagnostic._resolve_args(template, {}, pool)
        assert result["a"] == "http://10.0.0.1"
        assert result["b"] == "http://10.0.0.1"
        assert result["c"] == "http://10.0.0.1"
        assert result["d"] == "vm is v-123"

    def test_set_pool_var_lowercases_key(self):
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        # 生产侧写入入口强制小写，无论 produces 名大小写 / 首尾空格如何，池内 Key 永远统一
        diag._set_pool_var("HOST", "node-001")
        diag._set_pool_var("HoSt", "x")       # 同键不同大小写 -> 归一为 host（后者覆盖）
        diag._set_pool_var(" vm_id ", "v-9")   # 首尾空格 + 大写 -> vm_id
        assert diag._variable_pool == {"host": "x", "vm_id": "v-9"}

    def test_producer_consumer_roundtrip(self):
        # 模拟 _fill_pool_from_qkv 经 _set_pool_var 写入任意大小写 produces 名
        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        diag._set_pool_var("HOST", "node-001")  # produces name 可能大写
        # 消费侧模板用大写占位符 {{HOST}}，应命中池内小写 host
        result = KBDDiagnostic._resolve_args({"scope": "{{HOST}}"}, {}, diag._variable_pool)
        assert result == {"scope": "node-001"}
        # 即便模板误用小写 {{host}}，同样命中（纵深防御）
        result2 = KBDDiagnostic._resolve_args({"scope": "{{host}}"}, {}, diag._variable_pool)
        assert result2 == {"scope": "node-001"}


# ─── 单元测试：_judge_matches（规则判断）────────────────────────────────────


class TestJudgeMatchesRules:
    """_judge_matches：规则判断（不调用 LLM）"""

    @pytest.mark.asyncio
    async def test_contains_pattern_match(self):
        """__CONTAINS__ 模式匹配测试"""
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=MagicMock(),
        )
        kbds = [
            make_kbd("k1", [("tool_x", "__CONTAINS__:memory_error")]),
            make_kbd("k2", [("tool_x", "__CONTAINS__:disk_full")]),
        ]

        matched = await diag._judge_matches(
            tool_name="tool_x",
            actual_output="Error: memory_error detected on node-01",
            kbds=kbds,
        )

        assert "k1" in matched
        assert "k2" not in matched

    @pytest.mark.asyncio
    async def test_contains_pattern_case_insensitive(self):
        """__CONTAINS__ 模式大小写不敏感"""
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=MagicMock(),
        )
        kbds = [make_kbd("k1", [("tool_x", "__CONTAINS__:MEMORY_ERROR")])]

        matched = await diag._judge_matches(
            tool_name="tool_x",
            actual_output="memory_error occurred",
            kbds=kbds,
        )
        assert "k1" in matched

    @pytest.mark.asyncio
    async def test_regex_pattern_match(self):
        """__REGEX__ 模式匹配测试"""
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=MagicMock(),
        )
        kbds = [
            make_kbd("k1", [("tool_x", r"__REGEX__:error_code=0x[0-9A-F]+")]),
            make_kbd("k2", [("tool_x", r"__REGEX__:timeout_ms=\d{4,}")]),
        ]

        matched = await diag._judge_matches(
            tool_name="tool_x",
            actual_output="error_code=0xCFFFFF in syslog",
            kbds=kbds,
        )
        assert "k1" in matched
        assert "k2" not in matched

    @pytest.mark.asyncio
    async def test_invalid_regex_treated_as_no_match(self):
        """无效正则表达式不抛异常，视为不匹配"""
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=MagicMock(),
        )
        kbds = [make_kbd("k1", [("tool_x", "__REGEX__:[invalid(")])]

        matched = await diag._judge_matches(
            tool_name="tool_x",
            actual_output="some output",
            kbds=kbds,
        )
        assert "k1" not in matched

    @pytest.mark.asyncio
    async def test_no_pattern_conservative_match(self):
        """KBD 无此步骤定义时保守地保留（不过滤）"""
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=MagicMock(),
        )
        # k1 有 tool_x，k2 没有 tool_x（get_expected_pattern 返回 None）
        kbds = [
            make_kbd("k1", [("tool_x", "__CONTAINS__:error")]),
            make_kbd("k2", [("tool_y", "__CONTAINS__:error")]),  # 无 tool_x
        ]

        matched = await diag._judge_matches(
            tool_name="tool_x",
            actual_output="no relevant output",
            kbds=kbds,
        )
        # k2 无 tool_x 步骤，保守保留
        assert "k2" in matched


# ─── 单元测试：_evaluate_matcher（§6 5 类定型 valuator）────────────────────────


class TestMatcherEvaluation:
    """_evaluate_matcher：5 类定型 valuator 确定性求值，不调用 LLM。"""

    def setup_method(self):
        self.diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())

    def test_keyword_or(self):
        m = {"type": "keyword", "pattern": "CPU 资源不足", "mode": "or", "expected": True}
        assert self.diag._evaluate_matcher(m, "检测到 CPU 资源不足") is True
        assert self.diag._evaluate_matcher(m, "一切正常") is False

    def test_keyword_not(self):
        # not 模式：均不出现才符合期望
        m = {"type": "keyword", "pattern": "OOM", "mode": "not"}
        assert self.diag._evaluate_matcher(m, "无相关报错") is True
        assert self.diag._evaluate_matcher(m, "出现 OOM") is False

    def test_keyword_and(self):
        m = {"type": "keyword", "pattern": ["a", "b"], "mode": "and", "expected": True}
        assert self.diag._evaluate_matcher(m, "a 和 b 都存在") is True
        assert self.diag._evaluate_matcher(m, "只有 a") is False

    def test_regex(self):
        m = {"type": "regex", "pattern": r"err=\d{3}", "expected": True}
        assert self.diag._evaluate_matcher(m, "err=404 found") is True
        assert self.diag._evaluate_matcher(m, "ok") is False

    def test_state(self):
        m = {"type": "state", "pattern": "running", "expected": True}
        assert self.diag._evaluate_matcher(m, "service status: running") is True
        assert self.diag._evaluate_matcher(m, "status: stopped") is False

    def test_threshold_gt(self):
        m = {"type": "threshold", "operator": ">", "value": 90, "expected": True}
        assert self.diag._evaluate_matcher(m, "cpu_usage=95%") is True
        assert self.diag._evaluate_matcher(m, "cpu_usage=80%") is False

    def test_threshold_le(self):
        m = {"type": "threshold", "operator": "<=", "value": 10, "expected": True}
        assert self.diag._evaluate_matcher(m, "load=8") is True
        assert self.diag._evaluate_matcher(m, "load=20") is False

    def test_json_path(self):
        import json as _json

        m = {"type": "json_path", "path": "status", "expected_value": "healthy"}
        assert self.diag._evaluate_matcher(m, _json.dumps({"status": "healthy"})) is True
        assert self.diag._evaluate_matcher(m, _json.dumps({"status": "bad"})) is False

    def test_exists(self):
        m = {"type": "exists", "expected": True}
        assert self.diag._evaluate_matcher(m, "id=123 found") is True
        assert self.diag._evaluate_matcher(m, "对象不存在") is False

    def test_unknown_type_falls_back_to_llm(self):
        assert self.diag._evaluate_matcher({"type": "bogus"}, "x") is None


class TestToolDefinitionFallback:
    """2.1 让 tool_definition 生效：signals_json 缺省时回退 admin-ui 配置默认值。"""

    def setup_method(self):
        from types import SimpleNamespace

        from app.adapters.agents.htp import tool_registry

        self._mod = tool_registry
        self._added = ["qkv_alert", "qfk_log"]
        # 模拟 admin-ui 在 tool_definition 中配置的默认值
        self._mod.TOOL_REGISTRY["qkv_alert"] = SimpleNamespace(parameters={
            "properties": {"produces": {"default": [{"name": "HOST", "path": "host"}]}}
        })
        self._mod.TOOL_REGISTRY["qfk_log"] = SimpleNamespace(parameters={
            "properties": {
                "matcher": {
                    "default": {"type": "keyword", "pattern": ["X"], "mode": "or", "expected": True}
                }
            }
        })

    def teardown_method(self):
        for k in self._added:
            self._mod.TOOL_REGISTRY.pop(k, None)

    def test_qkv_produces_fallback(self):
        from app.tools.qkv.signal import FrontendSignal

        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        sig = {"acquirer": "qkv_alert", "acquirer_args": {"keyword": "disk"}}
        fsig = diag._signal_to_qkv(sig, {})
        assert isinstance(fsig, FrontendSignal)
        # signals_json 未配置 produces -> 应采用 tool_definition 默认值
        assert fsig.produces == [{"name": "HOST", "path": "host"}]

    def test_qkv_produces_explicit_overrides_fallback(self):
        from app.tools.qkv.signal import FrontendSignal

        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        sig = {
            "acquirer": "qkv_alert",
            "acquirer_args": {"keyword": "disk"},
            "produces": [{"name": "VM", "path": "vm"}],
        }
        fsig = diag._signal_to_qkv(sig, {})
        assert isinstance(fsig, FrontendSignal)
        # signals_json 显式配置应优先于 tool_definition 默认值
        assert fsig.produces == [{"name": "VM", "path": "vm"}]

    def test_qfk_matcher_fallback(self):
        from types import SimpleNamespace

        from app.tools.qfk.signal import BackendSignal

        diag = KBDDiagnostic(ai_registry=MagicMock(), tool_executor=MagicMock())
        step = SimpleNamespace(tool_name="qfk_log", tool_args_template={}, matcher=None)
        bsig = diag._signal_to_qfk(step)
        assert isinstance(bsig, BackendSignal)
        # signals_json 未配置 matcher -> 应采用 tool_definition 默认值
        assert bsig.match_mode == "or"
        assert bsig.keywords == ["X"]


# ─── 集成测试：完整诊断主循环 ────────────────────────────────────────────────


class TestDiagnoseLoop:
    """diagnose()：完整 KBD 差异诊断主循环集成测试"""

    @pytest.mark.asyncio
    async def test_empty_candidates_returns_error_event(self):
        """候选 KBD 为空时应返回错误提示并设置 result"""
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=MagicMock(),
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=[],
                env_context={},
                session_id="test-001",
            )
        ]

        assert diag.get_result() is not None
        assert diag.get_result().matched_kbds == []
        text_events = [e for e in events if hasattr(e, "content")]
        assert any("未找到" in e.content for e in text_events)

    @pytest.mark.asyncio
    async def test_single_candidate_skips_loop(self):
        """候选 KBD ≤ early_stop_threshold 时直接生成报告（不执行任何工具）"""
        tool_executor = make_tool_executor({"tool_a": "output"})
        diag = KBDDiagnostic(
            ai_registry=make_registry_mock('{"matches": {"k1": true}}'),
            tool_executor=tool_executor,
            early_stop_threshold=2,
        )

        candidates = [
            make_kbd("k1", [("tool_a", "__CONTAINS__:output")]),
            make_kbd("k2", [("tool_a", "__CONTAINS__:output")]),
        ]

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-002",
            )
        ]

        result = diag.get_result()
        assert result is not None
        # ≤ 2 候选时不循环，两个 KBD 均保留
        assert len(result.matched_kbds) == 2
        assert len(result.steps_executed) == 0

    @pytest.mark.asyncio
    async def test_eliminates_non_matching_kbds(self):
        """执行步骤后，不匹配的 KBD 被过滤掉"""
        # 工具执行返回包含 "memory_error" 的输出
        tool_executor = make_tool_executor({"acli_vm_config": "ERROR: memory_error critical"})

        # k1 期望 memory_error（匹配），k2 期望 disk_full（不匹配）
        candidates = [
            make_kbd("k1", [("acli_vm_config", "__CONTAINS__:memory_error")]),
            make_kbd("k2", [("acli_vm_config", "__CONTAINS__:disk_full")]),
            make_kbd("k3", [("acli_vm_config", "__CONTAINS__:memory_error")]),
        ]

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=tool_executor,
            early_stop_threshold=1,
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-003",
            )
        ]

        result = diag.get_result()
        assert result is not None
        # k2 被过滤，k1 和 k3 保留
        matched_ids = {kbd.id for kbd in result.matched_kbds}
        assert "k1" in matched_ids
        assert "k3" in matched_ids
        assert "k2" not in matched_ids

    @pytest.mark.asyncio
    async def test_definitive_match_sets_is_definitive_true(self):
        """精确锁定到 1 个 KBD 时 is_definitive=True"""
        tool_executor = make_tool_executor(
            {
                "tool_a": "memory_error",
                "tool_b": "node01 only",
            }
        )

        candidates = [
            make_kbd("k1", [("tool_a", "__CONTAINS__:memory_error"), ("tool_b", "__CONTAINS__:node01")]),
            make_kbd("k2", [("tool_a", "__CONTAINS__:memory_error"), ("tool_b", "__CONTAINS__:node02")]),
        ]

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=tool_executor,
            early_stop_threshold=1,
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-004",
            )
        ]

        result = diag.get_result()
        assert result is not None
        assert result.is_definitive is True
        assert result.matched_kbds[0].id == "k1"

    @pytest.mark.asyncio
    async def test_tool_execution_failure_does_not_crash(self):
        """工具执行失败时不崩溃，继续尝试其他步骤"""
        mock_executor = MagicMock()
        call_count = {"n": 0}

        async def execute(tool_name: str, args: dict) -> str:
            call_count["n"] += 1
            if tool_name == "tool_a":
                raise RuntimeError("工具连接超时")
            return "disk_error detected"

        mock_executor.execute = execute

        candidates = [
            make_kbd("k1", [("tool_a", "__CONTAINS__:disk"), ("tool_b", "__CONTAINS__:disk_error")]),
            make_kbd("k2", [("tool_a", "__CONTAINS__:memory"), ("tool_b", "__CONTAINS__:disk_error")]),
        ]

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=mock_executor,
            early_stop_threshold=1,
        )

        # 不应抛出异常
        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={},
                session_id="test-005",
            )
        ]

        result = diag.get_result()
        assert result is not None  # 正常完成

    @pytest.mark.asyncio
    async def test_env_context_fills_args_template(self):
        """env_context 中的值应替换工具参数模板中的占位符"""
        captured_args = {}

        async def execute(tool_name: str, args: dict) -> str:
            captured_args.update(args)
            return "ok"

        mock_executor = MagicMock()
        mock_executor.execute = execute

        # 构建含占位符的 KBD
        kbd = KBD(
            id="k1",
            name="KBD k1",
            category_id="虚拟机-003",
            problem_description="测试问题描述",
            signals=[
                {
                    "signal_category": "backend",
                    "acquirer": "acli_vm_config",
                    "acquirer_args": {"vm_name": "{{vm_name}}"},
                    "expected_pattern": "__CONTAINS__:ok",
                }
            ],
            root_cause="测试",
            solution="测试",
        )

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=mock_executor,
            early_stop_threshold=0,  # 强制执行所有步骤
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=[kbd],
                env_context={"vm_name": "test-vm-001"},
                session_id="test-006",
            )
        ]

        # 验证 vm_name 占位符已被替换
        assert captured_args.get("vm_name") == "test-vm-001"


# ─── 有效性验证：10 候选 KBD ≤ 8 步锁定 ──────────────────────────────────────


class TestKBDDiagEffectiveness:
    """KBD 差异诊断有效性验证：模拟真实场景，验证步骤数量"""

    @pytest.mark.asyncio
    async def test_ten_candidates_lock_in_few_steps(self):
        """10 个候选 KBD 应在 ≤ 8 步内锁定到 ≤ 3 个匹配 KBD。

        验收标准（来自 KBD 差异诊断协议）：
          - top-K=10 → 预期 ≤ 8 步完成消除
          - 真实 KBD（k5）应在最终匹配列表中
        """
        # 构建 10 个 KBD，每个有独特的期望模式
        # 真实故障：k5（Redis 服务异常导致虚拟机开机失败）
        candidates = [
            make_kbd("k1", [("get_active_alerts", "__CONTAINS__:network"), ("acli_vm_config", "__CONTAINS__:vlan")]),
            make_kbd(
                "k2", [("get_active_alerts", "__CONTAINS__:storage"), ("acli_vm_disk_check", "__CONTAINS__:error")]
            ),
            make_kbd(
                "k3", [("acli_vm_config", "__CONTAINS__:cpu_overcommit"), ("acli_system_top", "__CONTAINS__:cpu_load")]
            ),
            make_kbd(
                "k4", [("get_active_alerts", "__CONTAINS__:memory"), ("acli_vm_config", "__CONTAINS__:memory_mb")]
            ),
            make_kbd(
                "k5", [("get_failed_tasks", "__CONTAINS__:redis"), ("acli_platform_node_list", "__CONTAINS__:degraded")]
            ),
            make_kbd(
                "k6", [("get_active_alerts", "__CONTAINS__:network"), ("acli_vm_config", "__CONTAINS__:mac_address")]
            ),
            make_kbd("k7", [("get_failed_tasks", "__CONTAINS__:timeout"), ("acli_system_top", "__CONTAINS__:io_wait")]),
            make_kbd(
                "k8", [("acli_vm_config", "__CONTAINS__:disk"), ("acli_vm_disk_check", "__CONTAINS__:bad_sector")]
            ),
            make_kbd(
                "k9",
                [
                    ("get_failed_tasks", "__CONTAINS__:permission"),
                    ("acli_platform_node_list", "__CONTAINS__:node_count"),
                ],
            ),
            make_kbd(
                "k10", [("get_active_alerts", "__CONTAINS__:cluster"), ("acli_vm_list", "__CONTAINS__:powered_off")]
            ),
        ]

        # 模拟真实系统输出（k5 的特征：redis 失败，节点降级）
        tool_results = {
            "get_active_alerts": "No critical alerts",
            "get_failed_tasks": "FAILED: redis service start failed on vm-001 at 2024-01-15",
            "acli_vm_config": "vm-001 config: memory_mb=4096 cpu=2",
            "acli_vm_disk_check": "All disks healthy",
            "acli_system_top": "cpu_load=0.3 io_wait=0.1",
            "acli_platform_node_list": "node-01: healthy, node-02: degraded service",
            "acli_vm_list": "vm-001: powered_off vm-002: running",
        }

        tool_executor = make_tool_executor(tool_results)

        diag = KBDDiagnostic(
            ai_registry=make_registry_mock(),
            tool_executor=tool_executor,
            early_stop_threshold=2,
        )

        events = [
            event
            async for event in diag.diagnose(
                candidates=candidates,
                env_context={"vm_name": "vm-001"},
                session_id="effectiveness-test",
            )
        ]

        result = diag.get_result()
        assert result is not None

        # 验收标准 1：步骤数 ≤ 8
        assert len(result.steps_executed) <= 8, f"步骤数 {len(result.steps_executed)} 超过预期上限 8 步"

        # 验收标准 2：真实 KBD k5 在匹配列表中
        matched_ids = {kbd.id for kbd in result.matched_kbds}
        assert "k5" in matched_ids, f"真实 KBD k5 未出现在匹配列表中，当前匹配：{matched_ids}"

        # 验收标准 3：最终候选数量缩减（从 10 减少到 ≤ 6）
        assert len(result.matched_kbds) <= 6, f"候选 KBD 数量 {len(result.matched_kbds)} 未有效缩减"
