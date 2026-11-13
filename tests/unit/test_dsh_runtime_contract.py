"""DSH 离线诊断 Python 运行时契约测试。"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "dsh" / "Dockerfile"
ENTRYPOINT = ROOT / "dsh" / "docker-entrypoint.sh"


def test_dsh_image_builds_frozen_hci_python_runtime() -> None:
    """镜像必须在构建期生成锁定的 HCI Python venv，而不是依赖运行环境。"""
    dockerfile = DOCKERFILE.read_text()
    assert "python3 python3-pip python3-venv" in dockerfile
    assert "hci-offline-diagnosis-bundle-*.tgz" in dockerfile
    assert "uv sync --directory \"$runtime_dir\" --frozen --no-dev --no-editable" in dockerfile
    assert 'tar -xOf "$archive" package/package.json' in dockerfile


def test_dsh_entrypoint_fails_fast_when_hci_runtime_is_broken() -> None:
    """启动自检必须在首次企微消息前暴露运行时缺失。"""
    entrypoint = ENTRYPOINT.read_text()
    assert "HCI_BUNDLE_VERSION" in entrypoint
    assert "/opt/hci-offline-diagnosis/${HCI_BUNDLE_VERSION}/.venv/bin/python" in entrypoint
    assert "HCI_DIAGNOSIS_PYTHON 不存在或不可执行" in entrypoint
    assert "import hci_diagnosis, yaml, rank_bm25, mcp" in entrypoint
