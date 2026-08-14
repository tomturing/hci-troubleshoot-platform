"""Diagnosis Service 生产镜像的 Shared Runtime 导入门禁。"""

from pathlib import Path

import shared
from shared.resolution.catalog import load_acli_catalog


def test_resolution_runtime_has_acli_catalog_in_workspace():
    """离线同步不能因缺失 aCLI Catalog 而把合法命令全部判为未知。"""

    assert load_acli_catalog()


def test_dockerfile_does_not_overlay_root_shared_package():
    """服务目录中的历史 shared 副本不得覆盖根目录权威实现。"""

    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    assert "COPY backend/diagnosis-service /app" not in content
    assert "COPY backend/diagnosis-service/app /app/app" in content
    assert "COPY backend/shared /app/shared" in content


def test_root_shared_package_wins_over_service_local_copy():
    """即使开发目录残留历史副本，Python 也只能加载根目录 shared。"""

    shared_root = Path(shared.__file__).resolve().parent
    assert shared_root.name == "shared"
    assert shared_root.parent.name == "backend"
