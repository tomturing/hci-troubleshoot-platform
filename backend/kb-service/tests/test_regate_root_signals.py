"""存量根因信号「重新过门禁」工具单测（scripts/ops/regate_root_signals.py）。

守护点（为什么需要这些用例）：
- **幂等**：运行时准入闸门只看 provenance.gate_version/gate_status，重跑若破坏已盖章信号，
  人工审核结论会被工具悄悄改写；
- **拒收不静默**：L2 判死的信号必须产出可审计条目（reason_code），不能只从数组里消失；
- **已盖章信号不被二次剪枝**：L3 是「同篇内判据包含」的结构性剪枝，已通过人工确认的
  信号不能因为后来新增了更强信号而被工具删除；
- **输出保原序**：工具只改内容集合，不得改变审核页的展示顺序。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_ops_module() -> Any:
    """按文件路径加载 ops 工具（scripts/ 不是包，不能用常规 import）。"""
    path = REPO_ROOT / "scripts/ops/regate_root_signals.py"
    spec = importlib.util.spec_from_file_location("regate_root_signals", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ops() -> Any:
    return _load_ops_module()


def _signal(text: str, expected: list[str], *, evidence: str = "证据", gated: bool = False) -> dict[str, Any]:
    provenance: dict[str, Any] = {"evidence": evidence}
    if gated:
        provenance.update({"gate_version": 2, "gate_status": "passed", "gate_warnings": []})
    return {
        "schema_version": 2,
        "role": "root_cause",
        "match_type": "executable",
        "signal_text": text,
        "acquire": {
            "tool": "qfk_log",
            "args": {"file": "/var/log/sfvt_vtpdaemon.log", "host": "{{HOST}}"},
        },
        "match": {"type": "keyword", "mode": "all", "expected": expected},
        "provenance": provenance,
    }


class TestRegate:
    def test_stamps_ungated_signal_as_runtime_admissible(self, ops) -> None:
        kept, dropped, pruned = ops.regate([_signal("bitmap 分配失败", ["Create bitmap storage area error"])])
        assert dropped == [] and pruned == []
        assert len(kept) == 1
        provenance = kept[0]["provenance"]
        # 运行时闸门要求 gate_version>=2 且 gate_status=='passed'
        assert provenance["gate_version"] == 2
        assert provenance["gate_status"] == "passed"
        assert provenance["gate_warnings"] == []
        # 只盖 provenance，不改判定内容
        assert kept[0]["match"]["expected"] == ["Create bitmap storage area error"]

    def test_generic_error_strings_are_dropped_with_audit(self, ops) -> None:
        """kbd 1692 实锤形态：expected 全为通用错误串 → 拒收且可审计。"""
        kept, dropped, _ = ops.regate([_signal("任务锁失败", ["lock", "failed"])])
        assert kept == []
        assert len(dropped) == 1
        assert dropped[0]["reason_code"] == "insufficient_discrimination"
        assert dropped[0]["signal_text"] == "任务锁失败"
        assert "通用错误串" in dropped[0]["reason"]

    def test_config_field_tokens_warn_but_never_admit(self, ops) -> None:
        """疑似恒真只能警告：gate_status=warning 而**不是** passed（运行时精确等值比较）。"""
        kept, dropped, _ = ops.regate([_signal("网卡配置项", ["netmask", "gateway"])])
        assert dropped == [] and len(kept) == 1
        assert kept[0]["provenance"]["gate_status"] == "warning"
        assert kept[0]["provenance"]["gate_warnings"] == ["likely_always_true"]

    def test_already_gated_signal_is_never_rejudged(self, ops) -> None:
        """已盖章信号原样保留：即便它今天会被 L2 判死，也不得由工具擅自删除。"""
        gated = _signal("人工确认过的通用判据", ["lock", "failed"], gated=True)
        kept, dropped, pruned = ops.regate([gated])
        assert kept == [gated] and dropped == [] and pruned == []
        # 对象本身不被替换（保真：人审结论与字段原样）
        assert kept[0] is gated

    def test_idempotent_on_second_run(self, ops) -> None:
        first, _, _ = ops.regate([_signal("bitmap 分配失败", ["Create bitmap storage area error"])])
        second, dropped, pruned = ops.regate(first)
        assert dropped == [] and pruned == []
        assert second == first

    def test_redundant_signal_pruned_with_audit_payload(self, ops) -> None:
        """L3：弱判据 ⊂ 强判据 → 剪弱留强，剪枝项带 redundant_with 供审核人追溯。"""
        strong = _signal("强判据", ["Create bitmap storage area error", "size"], evidence="更完整的证据原文")
        weak = _signal("弱判据", ["size"], evidence="证据")
        kept, dropped, pruned = ops.regate([strong, weak])
        assert dropped == []
        assert [item["signal_text"] for item in kept] == ["强判据"]
        assert len(pruned) == 1
        assert pruned[0]["reason_code"] == "redundant_with_stronger_signal"
        assert pruned[0]["redundant_with"] == "强判据"

    def test_output_preserves_original_order(self, ops) -> None:
        """排序只用于判定谁先成为 stronger，输出必须保持调用方原序（不动审核页顺序）。"""
        first = _signal("第一条", ["alpha resource"])
        second = _signal("第二条", ["beta resource"])
        kept, _, _ = ops.regate([first, second])
        assert [item["signal_text"] for item in kept] == ["第一条", "第二条"]

    def test_mixed_batch_keeps_drop_and_prune_separated(self, ops) -> None:
        """同一批里三种去向并存：保留 / 拒收 / 剪枝 —— 互不污染。"""
        strong = _signal("强判据", ["Create bitmap storage area error", "size"], evidence="更完整的证据原文")
        weak = _signal("弱判据", ["size"], evidence="证据")
        bad = _signal("通用判据", ["lock", "failed"])
        kept, dropped, pruned = ops.regate([strong, weak, bad])
        assert [item["signal_text"] for item in kept] == ["强判据"]
        assert [item["signal_text"] for item in dropped] == ["通用判据"]
        assert [item["signal_text"] for item in pruned] == ["弱判据"]
