"""
反幻觉「硬拦截」契约测试（V-017 §3.3：缺证据来源的定量结论一律拦截，不再仅追加"待验证"标注）。

守护 Q2026100812343 复盘中"臆造 8001563222016 字节并被降级为待验证仍直出"的失败模式：
  - 检出无法溯源的数值（ungrounded_numbers）且处于结论语境 → 必须判定拦截；
  - 无未溯源数值 / 非结论语境 → 不得误拦（保持可用性）；
  - 受控替换文本不得包含任何具体数值，杜绝伪造定量结论。
"""

from __future__ import annotations

from app.adapters.agents.htp.react_engine import (
    CONCLUSION_MARKERS,
    UNGROUNDED_CONCLUSION_BLOCK_TEXT,
    ungrounded_conclusion_block,
)


def test_blocks_when_ungrounded_numbers_on_conclusion():
    content = "排障结论：该硬盘存在 8001563222016 字节的坏道数据。"
    report = {"ungrounded_numbers": ["8001563222016"], "has_hallucination": True}
    assert ungrounded_conclusion_block(content, report) is True


def test_no_block_without_ungrounded_numbers():
    content = "诊断结论：SMART 属性 05 超阈值，建议更换硬盘。"
    report = {"ungrounded_numbers": [], "has_hallucination": True}
    assert ungrounded_conclusion_block(content, report) is False


def test_no_block_when_not_conclusion_context():
    # 中间引导步骤出现无来源数值，但未处于结论语境，不触发硬拦截（交由既有 re-run/追注逻辑）
    content = "我先执行 lsblk 看看盘符。"
    report = {"ungrounded_numbers": ["123"]}
    assert ungrounded_conclusion_block(content, report) is False


def test_block_text_contains_no_fabricated_numbers():
    # 受控文本必须杜绝任何定量数字，避免替换后仍是幻觉
    assert not any(ch.isdigit() for ch in UNGROUNDED_CONCLUSION_BLOCK_TEXT)
    assert "受阻" in UNGROUNDED_CONCLUSION_BLOCK_TEXT
    assert "人工" in UNGROUNDED_CONCLUSION_BLOCK_TEXT


def test_conclusion_markers_cover_report_phrases():
    for phrase in ("排障结论", "根因确认", "诊断结论"):
        assert phrase in CONCLUSION_MARKERS
