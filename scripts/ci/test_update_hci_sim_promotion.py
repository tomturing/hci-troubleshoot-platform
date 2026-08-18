"""hci-sim promotion 清单更新器回归测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "update_hci_sim_promotion.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("update_hci_sim_promotion", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> str:
    return """metadata:
  annotations:
    hci-platform.dev/image-source-revision: "unverified-legacy"
spec:
  source:
    helm:
      values: |
        image:
          digest: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""


def test_updates_digest_and_source_revision_together():
    module = _load_module()
    updated = module.update_manifest(
        _manifest(),
        digest="sha256:" + "b" * 64,
        source_sha="c" * 40,
    )
    assert f'digest: "sha256:{"b" * 64}"' in updated
    assert f'image-source-revision: "{"c" * 40}"' in updated
    assert "unverified-legacy" not in updated


@pytest.mark.parametrize(
    ("digest", "source_sha"),
    [
        ("latest", "c" * 40),
        ("sha256:" + "b" * 63, "c" * 40),
        ("sha256:" + "b" * 64, "short"),
    ],
)
def test_rejects_invalid_identity(digest, source_sha):
    module = _load_module()
    with pytest.raises(ValueError):
        module.update_manifest(_manifest(), digest=digest, source_sha=source_sha)


def test_rejects_duplicate_digest_anchor():
    module = _load_module()
    duplicate = _manifest() + f'          digest: "sha256:{"d" * 64}"\n'
    with pytest.raises(ValueError, match="只能包含一个"):
        module.update_manifest(duplicate, digest="sha256:" + "b" * 64, source_sha="c" * 40)
