"""硬盘坏道 Live E2E 驱动层「纯逻辑」单测（不触网、不需 docker/LLM）。

driver/smoke 的网络编排只能在具备全栈的宿主机端到端实跑；但其中可脱网的解析/判定纯函数
（SSE 分派、终态信号扫描、已就绪变量提取、守卫 metric 键）在此固化为回归，防止缝隙解析退化。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluation.live_e2e.driver import (  # noqa: E402
    _dispatch_sse,
    _scan_terminal_signals,
    merge_observed_signals,
)
from evaluation.live_e2e.smoke import _resolved_variable_names  # noqa: E402


def test_dispatch_sse_interactive_request_not_treated_as_text():
    parts: list[str] = []
    payload = '{"kind":"variable_input","requestId":"r1","acpSessionId":"s1","metadata":{"variable_name":"disk_dev"}}'
    event, is_done, chunk = _dispatch_sse("interactive_request", payload, parts)
    assert event["kind"] == "variable_input"
    assert event["metadata"]["variable_name"] == "disk_dev"
    assert is_done is False
    assert chunk == "" and parts == []  # 交互卡不得污染助手文本


def test_dispatch_sse_text_chunk_accumulates():
    parts: list[str] = []
    _dispatch_sse(None, '{"content":"分析中"}', parts)
    _dispatch_sse(None, '{"content":"…完成"}', parts)
    assert parts == ["分析中", "…完成"]


def test_dispatch_sse_done_and_badjson():
    parts: list[str] = []
    _, is_done, _ = _dispatch_sse(None, "[DONE]", parts)
    assert is_done is True
    event, is_done, chunk = _dispatch_sse(None, "not-json", parts)
    assert is_done is False and event["kind"] == "raw"


def test_scan_terminal_signals_extracts_category_and_sop_id():
    events = [
        {"event": "message", "data": {"content": "x"}},
        {"event": "tool_result", "data": {"metadata": {"sop_document_id": 4, "category_code": "硬件-025"}}},
    ]
    category, sop = _scan_terminal_signals(events)
    assert category == "硬件-025" and sop == 4


def test_resolved_variable_names_only_non_empty():
    persisted = {"disk_dev": {"value": "/dev/sde"}, "node_ip": {"value": ""}, "asan_disks": "raw-text"}
    assert _resolved_variable_names(persisted) == {"disk_dev", "asan_disks"}


def test_merge_observed_signals_keeps_candidate_selection_over_none_scan():
    """回归守护（simrunM attempt1 假阴性）：候选点选已确证的分类码不得被 scan 返回的 None 冲掉。"""
    # S0 走候选澄清：driver 已在交互链路记录 observed_category_code，但终态事件不再带该字段
    assert merge_observed_signals("硬件-025", None, (None, None)) == ("硬件-025", None)
    # 已确证的 sop id 同理不被 None 覆盖
    assert merge_observed_signals(None, 4, (None, None)) == (None, 4)
    # 模型直接确认分类（不经点选）：live 为空时用扫描值兜底
    assert merge_observed_signals(None, None, ("硬件-025", 4)) == ("硬件-025", 4)
    # 两者都有值：live 优先保留
    assert merge_observed_signals("硬件-025", 4, ("其他-001", 9)) == ("硬件-025", 4)
