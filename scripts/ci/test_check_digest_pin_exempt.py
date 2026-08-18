"""check_digest_pin_exempt 的回归测试。

核心防护：历史 bug 为 git diff 的 digest 行带缩进空格（如 `-          digest: "sha256:..."`），
旧正则 `^[-+]?digest:` 因未允许「-」后空白而永远匹配失败，导致豁免失效。
本测试确保带缩进的纯 digest 钉入被正确豁免。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "check_digest_pin_exempt.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_digest_pin_exempt", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_digest_without_source_revision_is_not_exempt():
    """只改 digest 会丢失来源绑定，不再允许豁免。"""
    mod = _load()
    diff = (
        "--- a/deploy/gitops/argo-apps/local/hci-sim-dev.yaml\n"
        "+++ b/deploy/gitops/argo-apps/local/hci-sim-dev.yaml\n"
        "@@ -10,7 +10,7 @@ spec:\n"
        "           repository: ghcr.io/tomturing/hci-sim\n"
        '-          digest: "sha256:2419c2f8aef6f7b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7"\n'
        '+          digest: "sha256:503b905bd869953412d2f3db3ef3ca2ae447feac6209f8b4d1d0187413cc4d5b"\n'
    )
    assert mod.is_digest_pin_only(diff) is False


def test_digest_and_source_revision_promotion_is_exempt():
    """digest 与精确源码 revision 同步变化属于已文档化晋级动作。"""
    mod = _load()
    diff = (
        '-    hci-platform.dev/image-source-revision: "unverified-legacy"\n'
        '+    hci-platform.dev/image-source-revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        '-          digest: "sha256:2419c2f8aef6f7b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7"\n'
        '+          digest: "sha256:503b905bd869953412d2f3db3ef3ca2ae447feac6209f8b4d1d0187413cc4d5b"\n'
    )
    assert mod.is_digest_pin_only(diff) is True


def test_repository_change_is_not_exempt():
    """repository 也变了 → 不可豁免（属真实行为变更，须同步文档）。"""
    mod = _load()
    diff = (
        "-          repository: ghcr.io/tomturing/hci-sim\n"
        "+          repository: ghcr.io/tomturing/hci-sim-v2\n"
        '-          digest: "sha256:2419c2f8"\n'
        '+          digest: "sha256:503b905b"\n'
    )
    assert mod.is_digest_pin_only(diff) is False


def test_added_non_digest_field_is_not_exempt():
    """除 digest 外新增了其他字段 → 不可豁免。"""
    mod = _load()
    diff = '-          digest: "sha256:2419c2f8"\n+          digest: "sha256:503b905b"\n+          newField: true\n'
    assert mod.is_digest_pin_only(diff) is False


def test_unrelated_annotation_is_not_exempt():
    """不得把任意 Application annotation 伪装成晋级元数据绕过文档门禁。"""
    mod = _load()
    diff = (
        '-    hci-platform.dev/image-source-revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        '+    hci-platform.dev/image-source-revision: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"\n'
        '+    dangerous.example/bypass: "true"\n'
    )
    assert mod.is_digest_pin_only(diff) is False


def test_empty_diff_fails_closed():
    """diff 读取失败或空输入不能伪装成合法 promotion。"""
    mod = _load()
    assert mod.is_digest_pin_only("") is False
