"""根因信号链路服务端装配测试（与关键信号链路完全解耦）。

守护的架构不变量：

1. **纯新增**：本模块只产出 ``root_*`` 字段，不读写任何关键信号字段；
2. **自主准入**：根因信号的可执行性由根因自身门禁（gate_version / gate_status）
   判定，不继承关键信号的语义画像、发布章、工具契约版本等规则；
3. **运行时形态无轨道标记**：归一结果不带 ``track`` 键——轨道选择发生在
   agent-service 编排层，共享内核不感知「关键路 / 根因路」概念；
4. **契约可编译**：归一结果的 ``match.extract`` 必须能通过
   ``validate_publishable_signals_json``，否则全部根因 KBD 会静默退化为不可执行
   （历史缺陷：``rows:"all"`` + ``format:"text"`` 形态被契约驳回，实测 3 个可执行
   KBD 全灭）。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from app.services import root_signal_track as track
from shared.schemas.signal_schema import validate_publishable_signals_json
from shared.signals.matcher import evaluate_matcher

# 归一后必须与关键信号 keyword/regex 样本完全一致的取数契约
EXTRACT = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}


def _root(
    match_type: str,
    match: dict,
    *,
    tool: str = "qfk_system",
    signal_text: str = "根因特征",
    gate_version: int | None = 2,
    gate_status: str | None = "passed",
    args: dict | None = None,
) -> dict:
    """构造一条根因信号；默认带已过「最少充分集」门禁的 provenance。"""
    acquire = {"tool": tool, "args": args if args is not None else {"command": "dmesg"}} if match_type == "executable" else None
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
    )


# ── 0. 结构性：不反向依赖关键链路 ────────────────────────────────────────────
def test_module_does_not_import_playbooks_route():
    """根因装配层不得反向依赖关键信号路由（否则淘汰任一条都会牵连另一条）。"""
    source = inspect.getsource(track)
    assert "from app.routes" not in source
    assert "import playbooks" not in source


def test_module_does_not_use_track_marker():
    """运行时形态不含 track 标记：轨道选择属于编排层，不属于共享内核。"""
    source = inspect.getsource(track)
    assert '"track"' not in source
    assert "'track'" not in source


# ── 1. 读取 root_signal_json 的宽容度 ────────────────────────────────────────
def test_root_signals_of_accepts_v2_array():
    roots = [_root("text", {"type": "keyword", "expected": ["a"]})]
    assert track.root_signals_of(_entry(root_signal_json=roots)) == roots


def test_root_signals_of_wraps_v1_single_object():
    single = _root("text", {"type": "keyword", "expected": ["a"]})
    assert track.root_signals_of(_entry(root_signal_json=single)) == [single]


def test_root_signals_of_tolerates_missing_and_garbage():
    assert track.root_signals_of(SimpleNamespace()) == []
    assert track.root_signals_of(_entry(root_signal_json=["not-a-dict", 3, None])) == []


# ── 2. 门禁版本/状态判定 ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "gate_version,expected",
    [(2, 2), (3, 3), (1, 1), (None, 0), ("2", 2), ("bad", 0)],
)
def test_root_gate_version_normalization(gate_version, expected):
    root = _root("executable", {"type": "keyword", "expected": ["a"]}, gate_version=gate_version)
    assert track.root_gate_version(root) == expected


@pytest.mark.parametrize(
    "match_type,gate_version,gate_status,expected",
    [
        ("executable", 2, "passed", True),
        ("executable", 3, "passed", True),
        ("executable", 1, "passed", False),  # 未过最少充分集门禁
        ("executable", 2, "warning", False),  # 疑似恒真，仅留库
        ("executable", 2, None, False),
        ("text", 2, "passed", False),  # text 永不可执行
    ],
)
def test_is_runtime_eligible_truth_table(match_type, gate_version, gate_status, expected):
    root = _root(
        match_type,
        {"type": "keyword", "expected": ["a"]},
        gate_version=gate_version,
        gate_status=gate_status,
    )
    assert track.is_runtime_eligible(root) is expected


# ── 3. 根因简单 match → 运行时 CDD Matcher ───────────────────────────────────
def test_single_keyword_maps_to_keyword_and():
    cdd = track._match_to_cdd({"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})
    assert cdd == {
        "type": "keyword",
        "pattern": ["KVM_EXIT_SHUTDOWN"],
        "mode": "and",  # all 必须显式翻译，原样透传会落 else→or
        "expected": True,
        "extract": EXTRACT,
    }


def test_any_mode_maps_to_or():
    cdd = track._match_to_cdd({"type": "keyword", "mode": "any", "expected": ["alpha", "beta"]})
    assert cdd["mode"] == "or"


def test_multi_keyword_all_becomes_lookahead_regex():
    cdd = track._match_to_cdd({"type": "keyword", "mode": "all", "expected": ["invalid MAC address", "probe failed"]})
    assert cdd["type"] == "regex"
    assert "invalid\\ MAC\\ address" in cdd["pattern"]
    assert "probe\\ failed" in cdd["pattern"]
    assert cdd["extract"] == EXTRACT


@pytest.mark.parametrize(
    "output,expected",
    [
        # 两关键词分布在不同行 → all 语义命中
        ("header\nixgbe: probe failed here\neth0 invalid MAC address detected", True),
        # 缺一个 → 不得误判（逐行 keyword.and 只认同行同现，正是本设计要绕开的失真）
        ("only invalid MAC address here", False),
    ],
)
def test_multi_keyword_all_evaluates_faithfully_across_lines(output, expected):
    cdd = track._match_to_cdd({"type": "keyword", "mode": "all", "expected": ["invalid MAC address", "ixgbe: probe failed"]})
    assert evaluate_matcher(cdd, output).matched is expected


def test_regex_passthrough_uses_single_token():
    cdd = track._match_to_cdd({"type": "regex", "mode": "all", "expected": ["->\\s+/proc/\\d+/maps"]})
    assert cdd == {"type": "regex", "pattern": "->\\s+/proc/\\d+/maps", "expected": True, "extract": EXTRACT}


@pytest.mark.parametrize(
    "match",
    [
        {"type": "threshold", "mode": "all", "expected": ["83%", "88%"]},  # 数值型无法无损还原
        {"type": "keyword", "mode": "all", "expected": []},
        {"type": "keyword", "mode": "all", "expected": ["  ", ""]},
        {"type": "state", "mode": "all", "expected": ["running"]},
    ],
)
def test_non_convertible_match_is_dropped(match):
    assert track._match_to_cdd(match) is None


# ── 4. 归一结果形态 ────────────────────────────────────────────────────────
def test_normalize_shape_has_no_track_key_and_stable_id():
    root = _root("executable", {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]})
    norm = track.normalize_root_for_runtime(root)
    assert norm is not None
    assert set(norm) == {"id", "acquire", "match", "provenance"}
    assert norm["provenance"] == {"category": "backend"}
    assert norm["acquire"] == {"tool": "qfk_system", "args": {"command": "dmesg"}}
    assert "role" not in norm  # 交由 compile_signal_plan 归为 MUST，与关键信号同级
    assert norm["id"].startswith("root_")
    assert norm["id"] == track.normalize_root_for_runtime(root)["id"]


def test_normalize_keeps_supplied_id():
    root = {**_root("executable", {"type": "keyword", "expected": ["a"]}), "id": "root_explicit"}
    assert track.normalize_root_for_runtime(root)["id"] == "root_explicit"


def test_normalize_rejects_missing_tool():
    root = _root("executable", {"type": "keyword", "expected": ["a"]}, tool="")
    assert track.normalize_root_for_runtime(root) is None


# ── 5. 契约钉子：归一结果必须能被运行时契约接受（历史缺陷回归） ─────────────
def test_normalized_signal_passes_publishable_contract():
    """extract 形态若写成 rows:"all"/format:"text" 会被契约驳回 → 根因链路全灭。

    该缺陷曾在搬运实现时真实发生：合成数据跑不出，只有把归一结果送进
    validate_publishable_signals_json 才暴露。故此处用真实校验器钉死形态。
    """
    for match in (
        {"type": "keyword", "mode": "all", "expected": ["KVM_EXIT_SHUTDOWN"]},
        {"type": "keyword", "mode": "any", "expected": ["alpha", "beta"]},
        {"type": "regex", "mode": "all", "expected": ["probe.*failed"]},
    ):
        norm = track.normalize_root_for_runtime(_root("executable", match))
        assert norm is not None
        validate_publishable_signals_json({"schema_version": 2, "signals": [norm]})


def test_execution_issues_empty_for_valid_normalized_signal():
    norm = track.normalize_root_for_runtime(_root("executable", {"type": "keyword", "expected": ["a"]}))
    assert track._execution_issues([norm]) == []


def test_execution_issues_reports_duplicate_and_missing_fields():
    a = track.normalize_root_for_runtime(_root("executable", {"type": "keyword", "expected": ["a"]}))
    issues = track._execution_issues([a, dict(a)])
    assert any("重复" in item for item in issues)
    broken = {"id": "", "acquire": {}, "match": "not-a-dict"}
    issues = track._execution_issues([broken])
    assert any("缺少 signal_id" in item for item in issues)
    assert any("缺少 acquire.tool" in item for item in issues)
    assert any("缺少确定性 matcher" in item for item in issues)


def test_execution_issues_does_not_inherit_key_signal_rules():
    """仅含合法根因信号时必须判定为可执行，不得因「未配置关键信号」等规则阻断。"""
    norm = track.normalize_root_for_runtime(
        _root("executable", {"type": "keyword", "expected": ["a"]}, args={"command": "dmesg"})
    )
    assert track._execution_issues([norm]) == []


# ── 6. text 展示锚点 ───────────────────────────────────────────────────────
def test_display_anchors_only_text():
    entry = _entry(
        root_signal_json=[
            _root("text", {"type": "keyword", "mode": "all", "expected": ["低微码", "KVM_EXIT_SHUTDOWN"]}, signal_text="低微码致VM关机"),
            _root("executable", {"type": "keyword", "expected": ["x"]}, signal_text="可执行根因"),
        ]
    )
    anchors = track.display_anchors_of(entry)
    assert anchors == [{"signal_text": "低微码致VM关机", "keywords": ["低微码", "KVM_EXIT_SHUTDOWN"], "match_type": "keyword"}]


def test_display_anchors_accepts_scalar_expected():
    entry = _entry(root_signal_json=[_root("text", {"type": "keyword", "expected": "单值"})])
    assert track.display_anchors_of(entry)[0]["keywords"] == ["单值"]


# ── 7. 完整装配载荷 ────────────────────────────────────────────────────────
def test_payload_fields_are_all_root_prefixed():
    """纯新增契约：本模块产出的键必须全部 root_ 前缀，绝无关键信号字段。"""
    payload = track.build_root_track_payload(_entry())
    assert payload
    assert all(key.startswith("root_") for key in payload)


def test_payload_executable_kbd():
    entry = _entry(root_signal_json=[_root("executable", {"type": "keyword", "expected": ["a"]})])
    payload = track.build_root_track_payload(entry)
    assert payload["root_executable"] is True
    assert payload["root_recommendable"] is True
    assert payload["root_diagnosis_capability"] == "executable"
    assert payload["root_execution_issues"] == []
    assert payload["root_signal_stats"] == {"total": 1, "executable": 1, "text": 0, "gated_out": 0}
    assert payload["root_snapshot_id"].startswith("rootsig-")


def test_payload_text_only_kbd_is_reference_only():
    entry = _entry(root_signal_json=[_root("text", {"type": "keyword", "expected": ["低微码"]})])
    payload = track.build_root_track_payload(entry)
    assert payload["root_executable"] is False
    assert payload["root_recommendable"] is True
    assert payload["root_diagnosis_capability"] == "reference_only"
    assert payload["root_signals"] == []
    assert payload["root_signal_stats"]["gated_out"] == 0


def test_payload_without_roots_is_none_capability():
    payload = track.build_root_track_payload(_entry())
    assert payload["root_executable"] is False
    assert payload["root_recommendable"] is False
    assert payload["root_diagnosis_capability"] == "none"
    assert payload["root_snapshot_id"] == "rootsig-none"


def test_payload_counts_gated_out_signals():
    entry = _entry(
        root_signal_json=[
            _root("executable", {"type": "keyword", "expected": ["a"]}, gate_version=None, gate_status=None),
            _root("executable", {"type": "threshold", "expected": ["90%"]}, signal_text="数值"),
        ]
    )
    payload = track.build_root_track_payload(entry)
    assert payload["root_signals"] == []
    assert payload["root_signal_stats"] == {"total": 2, "executable": 0, "text": 0, "gated_out": 2}


def test_payload_snapshot_id_stable_and_set_sensitive():
    a = [_root("executable", {"type": "keyword", "expected": ["a"]})]
    b = [_root("executable", {"type": "keyword", "expected": ["b"]})]
    assert track.build_root_track_payload(_entry(root_signal_json=a))["root_snapshot_id"] == track.build_root_track_payload(
        _entry(root_signal_json=a)
    )["root_snapshot_id"]
    assert track.build_root_track_payload(_entry(root_signal_json=a))["root_snapshot_id"] != track.build_root_track_payload(
        _entry(root_signal_json=b)
    )["root_snapshot_id"]


def test_payload_mixed_kbd_keeps_both_tracks_visible():
    """同一 KBD 同时有可执行根因与 text 根因：两者各自呈现，互不吞并。"""
    entry = _entry(
        root_signal_json=[
            _root("executable", {"type": "keyword", "expected": ["a"]}, signal_text="可执行"),
            _root("text", {"type": "keyword", "expected": ["b"]}, signal_text="文本"),
        ]
    )
    payload = track.build_root_track_payload(entry)
    assert len(payload["root_signals"]) == 1
    assert len(payload["root_display_anchors"]) == 1
    assert payload["root_signal_stats"] == {"total": 2, "executable": 1, "text": 1, "gated_out": 0}
