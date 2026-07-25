"""HCI 中文分词测试。"""

from app.utils.jieba_hci import init_jieba, segment


def test_segment_splits_chinese_query_into_search_tokens():
    init_jieba()

    tokens = segment("虚拟机镜像异常").split()

    assert len(tokens) > 1
    assert "虚拟机" in tokens


def test_segment_preserves_hci_domain_terms():
    init_jieba()

    tokens = segment("peth0发生IO超时").split()

    assert "peth0" in tokens
    assert "IO超时" in tokens
