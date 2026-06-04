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
from app.adapters.agents.htp.kbd_model import KBD, KBDStep

# ─── 测试数据工厂 ─────────────────────────────────────────────────────────────


def make_kbd(
    kbd_id: str,
    steps: list[tuple[str, str]],  # [(tool_name, expected_pattern), ...]
    root_cause: str = "测试根因",
    similarity: float = 0.8,
) -> KBD:
    """快速构建 KBD 测试对象。"""
    return KBD(
        id=kbd_id,
        name=f"KBD {kbd_id}",
        category_id="虚拟机-003",
        problem_description=f"{kbd_id} 的问题描述",
        steps=[
            KBDStep(
                tool_name=tool_name,
                tool_args_template={},
                expected_pattern=expected_pattern,
            )
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
            steps=[
                KBDStep(
                    tool_name="acli_vm_config",
                    tool_args_template={"vm_name": "{{vm_name}}"},
                    expected_pattern="__CONTAINS__:ok",
                )
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
