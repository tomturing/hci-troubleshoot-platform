"""根因信号运行时两轨消费测试（PR B / #2）。

轨道一：executable 根因信号与关键信号同等并入 CDD 信号计划（仅非语义 live 路径）；
轨道二：text 根因信号仅作诊断报告「人工确认锚点」展示，绝不进 signals/门禁/召回。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import playbooks
from shared.signals.matcher import evaluate_matcher


def _extract() -> dict:
    return {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "value_mode": "string",
    }


def _root(match_type: str, match: dict, *, tool: str = "qfk_system", signal_text: str = "根因特征") -> dict:
    acquire = {"tool": tool, "args": {"command": "dmesg"}} if match_type == "executable" else None
    return {
        "schema_version": 2,
        "role": "root_cause",
        "match_type": match_type,
        "signal_text": signal_text,
        "acquire": acquire,
        "match": match,
        "provenance": {"source_section": "root_cause", "trace_tier": "root_cause", "evidence": signal_text},
    }


def _entry(*, signals_json=None, root_signal_json=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        support_id="S1",
        title="T",
        category_id="虚拟机-001",
        status="published",
        signals_json=signals_json if signals_json is not None else {"schema_version": 2, "signals": []},
        root_signal_json=root_signal_json if root_signal_json is not None else [],
    )


# ─── 轨道一：根因简单 match → 运行时 CDD Matcher 契约转换 ──────────────────────


def test_root_match_single_keyword_stays_keyword_with_runtime_mode():
    cdd = playbooks._root_match_to_cdd({"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})
    assert cdd["type"] == "keyword"
    assert cdd["pattern"] == ["KVM_EXIT_SHUTDOWN"]
    # 根因 all → 运行时 and（绝不能原样透传 all，否则 _normalize_mode 落到 else→or）
    assert cdd["mode"] == "and"
    assert cdd["expected"] is True
    assert cdd["extract"] == _extract()


def test_root_match_any_maps_to_or():
    cdd = playbooks._root_match_to_cdd({"type": "keyword", "mode": "any", "expected": ["alpha", "beta"]})
    assert cdd["type"] == "keyword"
    assert cdd["mode"] == "or"


def test_root_match_multi_keyword_all_becomes_lookahead_regex():
    cdd = playbooks._root_match_to_cdd({"type": "keyword", "mode": "all", "expected": ["invalid MAC address", "probe failed"]})
    assert cdd["type"] == "regex"
    assert cdd["expected"] is True
    assert cdd["extract"] == _extract()
    # 两个关键词都要在 pattern（转义后）里出现
    assert "invalid\\ MAC\\ address" in cdd["pattern"]
    assert "probe\\ failed" in cdd["pattern"]


def test_root_match_non_convertible_numeric_type_dropped():
    # 试点实测 executable 根因 100% keyword；数值型无法无损还原，保守丢弃
    assert playbooks._root_match_to_cdd({"type": "threshold", "mode": "all", "expected": ["80%"]}) is None
    assert playbooks._root_match_to_cdd({"type": "keyword", "mode": "all", "expected": []}) is None


def test_root_match_regex_passthrough_with_whole_stdout_extract():
    cdd = playbooks._root_match_to_cdd({"type": "regex", "mode": "all", "expected": ["->\\s+/proc/\\d+/maps"]})
    assert cdd == {"type": "regex", "pattern": "->\\s+/proc/\\d+/maps", "expected": True, "extract": _extract()}


# ─── 轨道一：忠实性——转换后的 matcher 在真实 evaluate_matcher 上跨行判定正确 ────


@pytest.mark.parametrize(
    "output,expected",
    [
        # 两个关键词分布在不同行，all 语义要求整段输出全部出现 → 命中
        ("header line\nixgbe: probe failed here\neth0 invalid MAC address detected", True),
        # 缺少其中一个 → 不误判命中（这正是逐行 keyword.and 会错、正则跨行才对的关键场景）
        ("only invalid MAC address here, nothing about probe", False),
    ],
)
def test_multi_keyword_all_faithfully_evaluates_across_lines(output, expected):
    cdd = playbooks._root_match_to_cdd(
        {"type": "keyword", "mode": "all", "expected": ["invalid MAC address", "ixgbe: probe failed"]}
    )
    assert evaluate_matcher(cdd, output).matched is expected


# ─── 轨道一：normalize_root_for_runtime 归一结果 ───────────────────────────────


def test_normalize_root_for_runtime_shape_and_stable_id():
    root = _root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})
    norm = playbooks.normalize_root_for_runtime(root)
    assert norm is not None
    assert set(norm) == {"id", "acquire", "match", "provenance"}
    assert norm["provenance"] == {"category": "backend"}
    assert norm["acquire"] == {"tool": "qfk_system", "args": {"command": "dmesg"}}
    # 不带 role：交由 compile_signal_plan 经 _required 归为 MUST（与关键信号同等 required）
    assert "role" not in norm
    # id 派生稳定且带 root_ 前缀，避免与关键信号 id 碰撞
    assert norm["id"].startswith("root_")
    assert norm["id"] == playbooks.normalize_root_for_runtime(root)["id"]


def test_normalize_root_for_runtime_drops_non_convertible_and_missing_tool():
    assert playbooks.normalize_root_for_runtime(_root("executable", {"type": "threshold", "mode": "all", "expected": ["x"]})) is None
    no_tool = _root("executable", {"type": "keyword", "mode": "all", "expected": ["x"]})
    no_tool["acquire"] = {"tool": "", "args": {}}
    assert playbooks.normalize_root_for_runtime(no_tool) is None


# ─── 轨道一：_signals 并集（仅 executable 根因进列表，text 不进） ───────────────


def test_signals_unions_executable_root_but_excludes_text_root():
    key_signal = {
        "id": "k1",
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": "core dump", "mode": "or", "expected": True, "extract": _extract()},
    }
    entry = _entry(
        signals_json={"schema_version": 2, "signals": [key_signal]},
        root_signal_json=[
            _root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]}, signal_text="可执行根因"),
            _root("text", {"type": "keyword", "mode": "all", "expected": ["低微码"]}, signal_text="文本根因"),
        ],
    )
    merged = playbooks._signals(entry)
    ids = [s["id"] for s in merged]
    assert "k1" in ids
    assert any(i.startswith("root_") for i in ids)
    assert len(merged) == 2  # 关键 1 + 可执行根因 1；text 不进
    # text 根因关键词绝不出现在并集里
    assert not any("低微码" in str(s) for s in merged)


def test_signals_only_executable_root_kbd_is_nonempty():
    entry = _entry(
        signals_json={"schema_version": 2, "signals": []},
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})],
    )
    merged = playbooks._signals(entry)
    assert len(merged) == 1 and merged[0]["id"].startswith("root_")


def test_signals_only_text_root_yields_empty_union():
    entry = _entry(
        signals_json={"schema_version": 2, "signals": []},
        root_signal_json=[_root("text", {"type": "keyword", "mode": "all", "expected": ["低微码"]})],
    )
    assert playbooks._signals(entry) == []


# ─── 轨道一：三态门禁（_execution_issues） ─────────────────────────────────────


def test_execution_gate_only_executable_root_is_executable():
    """仅根因 KBD：并集含合法 qfk executable → 不再报「未配置关键信号」，可执行。"""
    entry = _entry(
        signals_json={"schema_version": 2, "signals": []},
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})],
    )
    signals = playbooks._signals(entry)
    issues = playbooks._execution_issues(signals, {"schema_version": 2, "signals": []})
    assert issues == []


def test_execution_gate_both_kind_union_executable():
    key_signal = {
        "id": "k1",
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": "core dump", "mode": "or", "expected": True, "extract": _extract()},
    }
    entry = _entry(
        signals_json={"schema_version": 2, "signals": [key_signal]},
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})],
    )
    issues = playbooks._execution_issues(playbooks._signals(entry), {"schema_version": 2, "signals": [key_signal]})
    assert issues == []


def test_execution_gate_text_only_root_not_executable():
    """text 根因仅为展示锚点、不并入 signals，故有效运行时信号仍为空 → 非可执行。

    此时空原始文档命中 is_reference_only 前置早退（“仅案例推荐：无现场验证信号”），
    它比“未配置关键信号”更精确地表达“无可验证现场信号”语义；两路均阻断自动执行。
    """
    entry = _entry(
        signals_json={"schema_version": 2, "signals": []},
        root_signal_json=[_root("text", {"type": "keyword", "mode": "all", "expected": ["低微码"]})],
    )
    signals = playbooks._signals(entry)
    assert signals == []  # text 根因绝不进并集
    issues = playbooks._execution_issues(signals, {"schema_version": 2, "signals": []})
    assert issues  # 非空即不可执行
    assert any("不进入自动执行" in item or "未配置关键信号" in item for item in issues)


# ─── 轨道二：text 根因展示锚点 ─────────────────────────────────────────────────


def test_root_display_anchors_extracts_text_only():
    entry = _entry(
        root_signal_json=[
            _root("text", {"type": "keyword", "mode": "all", "expected": ["低微码", "KVM_EXIT_SHUTDOWN"]}, signal_text="低微码致VM关机"),
            _root("executable", {"type": "keyword", "mode": "all", "expected": ["x"]}, signal_text="可执行根因"),
        ]
    )
    anchors = playbooks._root_display_anchors(entry)
    assert len(anchors) == 1
    assert anchors[0]["signal_text"] == "低微码致VM关机"
    assert anchors[0]["keywords"] == ["低微码", "KVM_EXIT_SHUTDOWN"]


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_serialized_payload_exposes_display_anchor_without_touching_signals():
    key_signal = {
        "id": "k1",
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": "core dump", "mode": "or", "expected": True, "extract": _extract()},
    }
    entry = SimpleNamespace(
        id=1,
        support_id="S1",
        title="T",
        category_id="虚拟机-001",
        status="published",
        signals_json={"schema_version": 2, "signals": [key_signal]},
        root_signal_json=[_root("text", {"type": "keyword", "mode": "all", "expected": ["低微码"]}, signal_text="低微码致VM关机")],
        root_cause="RC",
        solution="SOL",
        problem_description="PD",
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_ScalarResult([]), _ScalarResult([entry])]), commit=AsyncMock()
    )
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))
    loader = MagicMock()
    loader.get_active = AsyncMock(return_value=SimpleNamespace())
    with (
        patch.object(playbooks, "_db_manager", db),
        patch.object(playbooks, "DynamicResourceLoader", return_value=loader),
        patch.object(playbooks, "snapshot_revision_metadata", return_value={"revision": 4}),
    ):
        response = await playbooks.get_category_playbooks("虚拟机-001")
    case = response["kbds"][0]
    assert case["root_display_anchors"] == [{"signal_text": "低微码致VM关机", "keywords": ["低微码"], "match_type": "keyword"}]
    # 关键：text 锚点绝不进 signals（不参与 compile/required/SATISFIED）
    assert case["signals"] == [key_signal]


@pytest.mark.asyncio
async def test_semantic_frozen_path_does_not_union_live_root():
    """Phase 1 语义/frozen 路径读取冻结快照 signals，绝不并入 live 根因，保持可复现契约。"""
    entry = SimpleNamespace(
        id=1,
        support_id="15936",
        title="live 工作稿",
        category_id="vm",
        status="published",
        signals_json={"schema_version": 2, "signals": []},
        # live 根因含一条 executable——语义路径必须忽略它
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})],
        root_cause="live rc",
        solution="live sol",
        problem_description="live pd",
    )
    frozen = SimpleNamespace(
        status="published",
        content={
            "title": "冻结标题",
            "category_id": "vm",
            "status": "published",
            "solution": "冻结方案",
            "signals_json": {
                "schema_version": 2,
                "signals": [],
                "semantic_entry_profile": {"diagnosis_capability": "reference_only"},
            },
        },
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_ScalarResult([]), _ScalarResult([entry])]), commit=AsyncMock()
    )
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))
    loader = MagicMock()
    loader.get_active = AsyncMock(return_value=frozen)
    with (
        patch.object(playbooks, "_db_manager", db),
        patch.object(playbooks, "DynamicResourceLoader", return_value=loader),
        patch.object(playbooks, "snapshot_revision_metadata", return_value={"revision": 8}),
    ):
        response = await playbooks.get_category_playbooks("vm")
    case = response["kbds"][0]
    # 语义冻结路径 signals 来自 frozen（空），live executable 根因未被并入
    assert not any(str(s.get("id", "")).startswith("root_") for s in case["signals"])
    assert case["executable"] is False
