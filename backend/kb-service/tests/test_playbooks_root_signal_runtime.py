"""关键信号链路的零侵入证明 + 根因链路的独立装配证明。

本文件回答两个架构问题（与被测实现的历史包袱无关）：

1. **删除根因链路会怎样？** —— ``playbooks.py`` 中根因链路只有一行
   ``**build_root_track_payload(kbd)``。移除它只会让 ``root_*`` 字段消失，
   其余字段逐字不变。故本文件用「同一 KBD 有无根因信号的序列化结果 diff
   必须为空」来钉死这一点。
2. **根因链路会不会偷偷改变关键链路的结论？** —— 不会：``_signals`` 只读
   ``signals_json``，``_execution_issues`` 只吃关键信号文档，根因信号从不
   进入这两条路径。

根因信号自身的归一/门禁/锚点逻辑在 ``test_root_signal_track.py`` 中测试。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import playbooks

KEY_FIELDS = (
    "id",
    "kbd_id",
    "support_id",
    "name",
    "title",
    "category_id",
    "status",
    "executable",
    "execution_issues",
    "signals",
    "verification_contract",
    "generation_metadata",
    "publish_validation",
    "semantic_entry_profile",
    "diagnosis_capability",
    "recommendable",
    "root_cause",
    "solution",
    "problem_description",
    "resource_revision",
)


def _extract() -> dict:
    return {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "value_mode": "string",
    }


def _key_signal() -> dict:
    return {
        "id": "k1",
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": "core dump", "mode": "or", "expected": True, "extract": _extract()},
    }


def _root(
    match_type: str,
    match: dict,
    *,
    tool: str = "qfk_system",
    signal_text: str = "根因特征",
    gate_version: int | None = 2,
    gate_status: str | None = "passed",
) -> dict:
    acquire = {"tool": tool, "args": {"command": "dmesg"}} if match_type == "executable" else None
    provenance: dict = {"source_section": "root_cause", "trace_tier": "root_cause", "evidence": signal_text}
    if gate_version is not None:
        provenance["gate_version"] = gate_version
    if gate_status is not None:
        provenance["gate_status"] = gate_status
    return {
        "schema_version": 2,
        "role": "root_cause",
        "match_type": match_type,
        "signal_text": signal_text,
        "acquire": acquire,
        "match": match,
        "provenance": provenance,
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
        root_cause="RC",
        solution="SOL",
        problem_description="PD",
    )


# ── 1. _signals 只读关键信号 ────────────────────────────────────────────────
def test_signals_never_unions_root_signals():
    entry = _entry(
        signals_json={"schema_version": 2, "signals": [_key_signal()]},
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})],
    )
    assert playbooks._signals(entry) == [_key_signal()]


def test_signals_empty_when_only_root_present():
    """仅有根因信号的 KBD 对关键链路而言仍是「无信号」——不因根因而变可执行。"""
    entry = _entry(
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})]
    )
    assert playbooks._signals(entry) == []


def test_signals_tolerates_non_dict_signals_json():
    entry = _entry(signals_json=["garbage"], root_signal_json=[])
    assert playbooks._signals(entry) == []


# ── 2. _execution_issues 不因根因信号改变 ───────────────────────────────────
def test_execution_issues_ignore_root_signals_for_key_track():
    """仅有可执行根因的 KBD：关键链路仍判不可执行（根因可执行性只体现在 root_*）。"""
    entry = _entry(
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})]
    )
    issues = playbooks._execution_issues(
        playbooks._signals(entry), entry.signals_json, support_id="S1", category_id="虚拟机-001"
    )
    assert issues


def test_execution_issues_identical_with_or_without_root_signals():
    key_doc = {"schema_version": 2, "signals": [_key_signal()]}
    with_root = _entry(
        signals_json=key_doc,
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})],
    )
    without_root = _entry(signals_json=key_doc)
    kwargs = {"support_id": "S1", "category_id": "虚拟机-001"}
    assert playbooks._execution_issues(playbooks._signals(with_root), with_root.signals_json, **kwargs) == (
        playbooks._execution_issues(playbooks._signals(without_root), without_root.signals_json, **kwargs)
    )


# ── 3. 序列化载荷：零侵入 + root_* 纯新增 ───────────────────────────────────
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


async def _serialize(entry: SimpleNamespace, *, frozen=None) -> dict:
    """走真实路由装配（db / 资源加载器均已 mock）。"""
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_ScalarResult([]), _ScalarResult([entry])]), commit=AsyncMock()
    )
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))
    loader = MagicMock()
    loader.get_active = AsyncMock(return_value=frozen or SimpleNamespace())
    with (
        patch.object(playbooks, "_db_manager", db),
        patch.object(playbooks, "DynamicResourceLoader", return_value=loader),
        patch.object(playbooks, "snapshot_revision_metadata", return_value={"revision": 4}),
    ):
        response = await playbooks.get_category_playbooks(entry.category_id)
    return response["kbds"][0]


@pytest.mark.asyncio
async def test_payload_exposes_full_root_track_fields():
    entry = _entry(
        signals_json={"schema_version": 2, "signals": [_key_signal()]},
        root_signal_json=[
            _root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]}, signal_text="关机"),
            _root("text", {"type": "keyword", "mode": "all", "expected": ["低微码"]}, signal_text="低微码致VM关机"),
        ],
    )
    case = await _serialize(entry)

    assert case["root_executable"] is True
    assert case["root_recommendable"] is True
    assert case["root_diagnosis_capability"] == "executable"
    assert case["root_execution_issues"] == []
    assert case["root_signal_stats"] == {"total": 2, "executable": 1, "text": 1, "gated_out": 0}
    # 运行时形态不带 track 标记（轨道选择属于编排层）
    assert set(case["root_signals"][0]) == {"id", "acquire", "match", "provenance"}
    # text 锚点为独立字段
    assert case["root_display_anchors"] == [{"signal_text": "低微码致VM关机", "keywords": ["低微码"], "match_type": "keyword"}]


@pytest.mark.asyncio
async def test_key_fields_are_byte_identical_with_or_without_root_signals():
    """零侵入的强证明：只切换 root_signal_json，20 个关键字段逐字不变。"""
    signals_json = {"schema_version": 2, "signals": [_key_signal()]}
    baseline = await _serialize(_entry(signals_json=signals_json))
    with_roots = await _serialize(
        _entry(
            signals_json=signals_json,
            root_signal_json=[
                _root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]}),
                _root("text", {"type": "keyword", "mode": "all", "expected": ["低微码"]}),
            ],
        )
    )
    for field in KEY_FIELDS:
        assert with_roots[field] == baseline[field], f"关键信号字段被根因信号污染: {field}"

    # 反向：新增字段全部 root_ 前缀，且 baseline 也具备（结构恒等，仅取值不同）
    extra_new = set(with_roots) - set(baseline)
    assert extra_new == set()
    assert all(key.startswith("root_") for key in with_roots if key not in KEY_FIELDS)


@pytest.mark.asyncio
async def test_text_anchor_never_leaks_into_signals():
    entry = _entry(
        signals_json={"schema_version": 2, "signals": [_key_signal()]},
        root_signal_json=[_root("text", {"type": "keyword", "mode": "all", "expected": ["低微码"]})],
    )
    case = await _serialize(entry)
    assert case["signals"] == [_key_signal()]
    assert not any("低微码" in str(signal) for signal in case["signals"])


@pytest.mark.asyncio
async def test_executable_root_never_leaks_into_signals():
    entry = _entry(
        signals_json={"schema_version": 2, "signals": [_key_signal()]},
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})],
    )
    case = await _serialize(entry)
    assert case["signals"] == [_key_signal()]
    assert not any(str(signal.get("id", "")).startswith("root_") for signal in case["signals"])


@pytest.mark.asyncio
async def test_semantic_frozen_path_ignores_live_root_signals():
    """语义冻结路径的关键字段来自冻结快照，live 根因信号不改变它。"""
    live = _entry(
        signals_json={"schema_version": 2, "signals": []},
        root_signal_json=[_root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})],
    )
    live.category_id = "vm"
    live.support_id = "15936"
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
    case = await _serialize(live, frozen=frozen)

    # 关键字段来自冻结快照
    assert case["signals"] == []
    assert case["name"] == "冻结标题"
    assert case["executable"] is False
    # root_* 描述 live 根因（不参与语义路径判定）
    assert case["root_executable"] is True


def test_playbooks_root_hook_is_single_expansion():
    """装配点必须只有一处展开：便于淘汰时一行删除。"""
    import inspect

    source = inspect.getsource(playbooks)
    assert source.count("build_root_track_payload(") == 1
