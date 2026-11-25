"""DSH WeKnora 诊断 bundle 运行时契约测试。"""

from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "dsh" / "Dockerfile"
ENTRYPOINT = ROOT / "dsh" / "docker-entrypoint.sh"
PLUGIN_DIR = ROOT / "dsh" / "plugins"


def test_repository_contains_installable_weknora_bundle() -> None:
    """入库制品必须是单个可直装的 WeKnora npm pack。"""
    archives = sorted(PLUGIN_DIR.glob("hci-weknora-diagnosis-bundle-*.tgz"))
    assert len(archives) == 1
    assert not list(PLUGIN_DIR.glob("hci-offline-diagnosis-bundle-*.tgz"))

    with tarfile.open(archives[0], "r:gz") as archive:
        manifest_file = archive.extractfile("package/package.json")
        assert manifest_file is not None
        manifest = json.load(manifest_file)

    assert manifest["name"] == "hci-weknora-diagnosis-bundle"
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"


def test_shipped_plugin_references_exist_in_repository() -> None:
    """部署清单引用的本地 tgz 必须真实入库，否则 Pod 启动安装插件时才 ENOENT。"""
    referenced: set[str] = set()
    for relative in (".env.example", "deploy/docker/docker-compose.yml"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        referenced.update(re.findall(r"/opt/dsh-plugins/([A-Za-z0-9._-]+\.tgz)", text))

    assert referenced
    missing = sorted(name for name in referenced if not (PLUGIN_DIR / name).is_file())
    assert not missing, f"清单引用了不存在的制品: {missing}"


def test_dsh_image_validates_weknora_bundle_manifest() -> None:
    """镜像必须在构建期拒绝无标准 npm manifest 的 WeKnora 离线整包。"""
    dockerfile = DOCKERFILE.read_text()
    assert "hci-weknora-diagnosis-bundle-*.tgz" in dockerfile
    assert 'tar -xOf "$archive" package/package.json' in dockerfile
    assert 'p.name !== "hci-weknora-diagnosis-bundle"' in dockerfile
    assert "p.dsh?.bundle?.patch" in dockerfile
    assert "hci-offline-diagnosis-bundle" not in dockerfile
    assert "uv sync" not in dockerfile


def test_dsh_entrypoint_uses_generic_profile_bundle_installation() -> None:
    """入口脚本只按部署清单安装 bundle，不再推导旧 HCI Python 运行时。"""
    entrypoint = ENTRYPOINT.read_text()
    assert 'dsh plugin --profile "$DSH_PROFILE" add "$PACKAGE"' in entrypoint
    assert "hci-offline-diagnosis-bundle" not in entrypoint
    assert "HCI_DIAGNOSIS_PYTHON" not in entrypoint
