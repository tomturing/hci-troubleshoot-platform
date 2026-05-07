"""
针对 2026-05-07 htp 大脑诊断检出质量修复的单元测试

当前文件实际覆盖的内容：
- N-1: Jaccard bigram 中文相似度
- P2: acli 命令只读判定

说明：
- 其余修复点（N-2/N-3/D-3/N-4）由集成测试或其他测试文件覆盖，不在此
  文件中声明，以避免对单元测试覆盖范围产生误解。
"""
import os
import sys

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

import pytest
from app.services.conversation_service import (
    _acli_is_readonly,
    _bigram_tokens,
    _extract_acli_commands,
    jaccard_similarity,
)

# ─── N-1: Jaccard bigram 中文相似度 ─────────────────────────────────────────

class TestBigramTokens:
    def test_english_bigrams(self):
        result = _bigram_tokens("hello")
        # "hello" → {he, el, ll, lo}
        assert result == {"he", "el", "ll", "lo"}

    def test_chinese_bigrams(self):
        result = _bigram_tokens("虚拟机")
        # "虚拟机" → {虚拟, 拟机}
        assert result == {"虚拟", "拟机"}

    def test_single_char(self):
        assert _bigram_tokens("A") == {"a"}

    def test_empty_string(self):
        assert _bigram_tokens("") == set()

    def test_lowercase_normalization(self):
        assert _bigram_tokens("AB") == _bigram_tokens("ab")


class TestJaccardSimilarity:
    def test_identical_chinese(self):
        score = jaccard_similarity("虚拟机开机失败", "虚拟机开机失败")
        assert score == pytest.approx(1.0)

    def test_similar_chinese(self):
        score = jaccard_similarity("虚拟机开机失败", "VM开机失败了")
        # 即使不完全一样，bigram 应有一定重叠（至少不是 0）
        assert score > 0.0

    def test_empty_strings(self):
        assert jaccard_similarity("", "abc") == 0.0
        assert jaccard_similarity("abc", "") == 0.0

    def test_completely_different(self):
        score = jaccard_similarity("AAAA", "BBBB")
        assert score == pytest.approx(0.0)

    def test_english_similarity(self):
        score = jaccard_similarity("hello world", "hello world")
        assert score == pytest.approx(1.0)

    def test_partial_match(self):
        # "abcd" bigrams: {ab, bc, cd}; "abce" bigrams: {ab, bc, ce}
        score = jaccard_similarity("abcd", "abce")
        # 交集: {ab, bc} = 2; 并集: {ab, bc, cd, ce} = 4
        assert score == pytest.approx(2 / 4)


# ─── P2: S3 acli 命令提取 ────────────────────────────────────────────────────

class TestExtractAcliCommands:
    def test_extract_from_fenced_code_block(self):
        text = """
请执行以下命令查看节点状态：

```bash
acli task get --id 12345
```

然后检查结果。
"""
        cmds = _extract_acli_commands(text)
        assert cmds == ["acli task get --id 12345"]

    def test_extract_from_inline_code(self):
        text = "运行 `acli node list` 命令查看所有节点"
        cmds = _extract_acli_commands(text)
        assert cmds == ["acli node list"]

    def test_extract_multiple_commands(self):
        text = """
首先: `acli task get --id 1`
然后:
```
acli node show --name worker-1
```
"""
        cmds = _extract_acli_commands(text)
        assert len(cmds) == 2
        assert "acli task get --id 1" in cmds
        assert "acli node show --name worker-1" in cmds

    def test_deduplicate_commands(self):
        text = "`acli task get --id 1`\n`acli task get --id 1`"
        cmds = _extract_acli_commands(text)
        assert len(cmds) == 1

    def test_no_acli_commands(self):
        text = "这是普通文本，没有命令"
        cmds = _extract_acli_commands(text)
        assert cmds == []

    def test_empty_text(self):
        assert _extract_acli_commands("") == []


class TestAcliIsReadonly:
    def test_get_is_readonly(self):
        assert _acli_is_readonly("acli task get --id 123") is True

    def test_list_is_readonly(self):
        assert _acli_is_readonly("acli node list") is True

    def test_show_is_readonly(self):
        assert _acli_is_readonly("acli task show --name foo") is True

    def test_status_is_readonly(self):
        assert _acli_is_readonly("acli vm status") is True

    def test_delete_is_not_readonly(self):
        assert _acli_is_readonly("acli task delete --id 123") is False

    def test_create_is_not_readonly(self):
        assert _acli_is_readonly("acli node create --name new-node") is False

    def test_update_is_not_readonly(self):
        assert _acli_is_readonly("acli vm update --memory 8") is False

    def test_restart_is_not_readonly(self):
        assert _acli_is_readonly("acli service restart nginx") is False
