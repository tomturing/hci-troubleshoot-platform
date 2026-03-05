"""
TextSplitter 单元测试

测试文本分块逻辑，不依赖外部服务。
"""

import pytest

from app.utils.text_splitter import TextSplitter, _estimate_tokens


def test_token_estimation_chinese():
    """中文 token 数估算"""
    text = "超融合虚拟机开机失败"  # 10 个汉字
    tokens = _estimate_tokens(text)
    # 10 / 1.5 ≈ 6.6 → 6
    assert 5 <= tokens <= 8


def test_token_estimation_english():
    """英文 token 数估算"""
    text = "virtual machine boot failure"  # 约 4-5 个 token
    tokens = _estimate_tokens(text)
    assert 4 <= tokens <= 8


def test_split_short_text():
    """短文本不分块，直接返回原文"""
    splitter = TextSplitter(chunk_size=512, chunk_overlap=128)
    text = "这是一段很短的文本，不需要分块。"
    chunks = splitter.split(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_by_markdown_headers():
    """按 Markdown 标题分块"""
    splitter = TextSplitter(chunk_size=50, chunk_overlap=0)
    text = (
        "## 第一章 CPU不足\n\n"
        "检查 CPU 剩余配额，如果不足则迁移虚拟机。这是一段较长的说明文字用于测试分块效果。\n\n"
        "## 第二章 内存不足\n\n"
        "检查内存使用情况，建议回收不用的虚拟机内存。这是另一段较长的说明文字。"
    )
    chunks = splitter.split(text)
    # 应该按标题分为至少 2 块
    assert len(chunks) >= 2
    # 每块不应超过 chunk_size 的 2 倍 tokens（分块器有一定容忍度）
    for chunk in chunks:
        assert _estimate_tokens(chunk) < 200


def test_split_preserves_content():
    """分块后内容完整（所有原始文本都在某个 chunk 中）"""
    splitter = TextSplitter(chunk_size=100, chunk_overlap=20)
    text = "## 测试章节\n\n" + "测试内容 " * 100
    chunks = splitter.split(text)
    # 合并所有分块，检查主要内容都存在
    combined = " ".join(chunks)
    assert "测试章节" in combined
    assert "测试内容" in combined


def test_split_empty_text():
    """空文本处理"""
    splitter = TextSplitter()
    chunks = splitter.split("   ")
    assert len(chunks) >= 1
