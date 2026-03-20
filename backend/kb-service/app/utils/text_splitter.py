"""
RecursiveCharacterTextSplitter — 递归字符文本分块器

设计说明：
- 按 CHUNK_SIZE（tokens）分块，CHUNK_OVERLAP 重叠，避免知识点被切断
- 优先按 Markdown 分隔符（## / ### / \n\n）分块，保留语义完整性
- token 估算：中文约 1.5 字符/token，英文约 4 字符/token（粗略估算，非精确）

参考：LangChain RecursiveCharacterTextSplitter 的策略，但不引入 LangChain 依赖。
"""

from __future__ import annotations

import re

# Markdown 分隔符优先级（从粗到细）
_SEPARATORS = [
    "\n## ",     # H2 标题（最粗粒度）
    "\n### ",    # H3 标题
    "\n#### ",   # H4 标题
    "\n\n",      # 段落
    "\n",        # 换行
    "。",        # 中文句号
    "；",        # 中文分号
    ". ",        # 英文句号
    " ",         # 空格
    "",          # 字符级（最后手段）
]


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（不引入 tiktoken 依赖）

    规则：
    - ASCII 字符：约 4 字符/token（英文单词平均长度）
    - 中文/日文/韩文字符：约 1.5 字符/token（每个汉字约 2/3 个 token）
    - 其他字符按 1 字符/token 计

    注意：此估算误差约 ±20%，可接受（分块大小不必精确）。
    """
    ascii_count = sum(1 for c in text if ord(c) < 128)
    cjk_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_count = len(text) - ascii_count - cjk_count
    return int(ascii_count / 4 + cjk_count / 1.5 + other_count)


def _split_text_recursive(text: str, separators: list[str], chunk_size: int) -> list[str]:
    """递归分割文本，优先使用高优先级分隔符"""
    # 找到第一个有效的分隔符
    separator = ""
    new_separators = []

    for i, sep in enumerate(separators):
        if sep == "" or sep in text:
            separator = sep
            new_separators = separators[i + 1:]
            break

    if not separator:
        # 没有有效分隔符，按字符切
        return [text[i: i + chunk_size * 2] for i in range(0, len(text), chunk_size * 2)]

    # 按分隔符切割
    if separator:
        splits = re.split(re.escape(separator), text)
        # 恢复分隔符（Markdown 标题需保留 # 前缀）
        if separator.startswith("\n#"):
            splits = [splits[0]] + [separator.lstrip("\n") + s for s in splits[1:]]
    else:
        splits = list(text)

    # 合并短片段，确保每块 ≤ chunk_size tokens
    good_splits: list[str] = []
    current_chunk = ""

    for split in splits:
        if not split.strip():
            continue
        candidate = current_chunk + ("\n" if current_chunk else "") + split
        if _estimate_tokens(candidate) <= chunk_size:
            current_chunk = candidate
        else:
            if current_chunk:
                good_splits.append(current_chunk.strip())
            # 如果单个 split 本身就超了，递归处理
            if _estimate_tokens(split) > chunk_size and new_separators:
                good_splits.extend(_split_text_recursive(split, new_separators, chunk_size))
            else:
                current_chunk = split

    if current_chunk:
        good_splits.append(current_chunk.strip())

    return good_splits


class TextSplitter:
    """文档分块器

    Usage:
        splitter = TextSplitter(chunk_size=512, chunk_overlap=128)
        chunks = splitter.split(markdown_text)
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 128):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> list[str]:
        """将文本分割为块列表

        Args:
            text: 原始 Markdown 文本

        Returns:
            分块文本列表（每块约 chunk_size tokens，相邻块有 chunk_overlap 重叠）
        """
        # 预处理：清理多余空行
        text = re.sub(r"\n{3,}", "\n\n", text.strip())

        # 递归分块
        raw_chunks = _split_text_recursive(text, _SEPARATORS, self.chunk_size)

        if not raw_chunks:
            return [text]

        # 添加重叠：相邻块间加入前一块的最后 overlap tokens 内容
        if self.chunk_overlap == 0 or len(raw_chunks) <= 1:
            return raw_chunks

        result: list[str] = [raw_chunks[0]]
        for i in range(1, len(raw_chunks)):
            prev = raw_chunks[i - 1]
            curr = raw_chunks[i]
            # 取前一块的后半部分作为重叠
            prev_tokens = _estimate_tokens(prev)
            if prev_tokens > self.chunk_overlap:
                # 按比例截取末尾
                keep_ratio = self.chunk_overlap / prev_tokens
                keep_chars = int(len(prev) * keep_ratio)
                overlap_text = prev[-keep_chars:].strip()
                result.append(overlap_text + "\n" + curr if overlap_text else curr)
            else:
                result.append(prev + "\n" + curr)

        return result
