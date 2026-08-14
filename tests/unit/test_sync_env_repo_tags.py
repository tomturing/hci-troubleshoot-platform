from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "sync-env-repo-tags.sh"


def run_sync(tmp_path: Path, values: str, services: str) -> subprocess.CompletedProcess[str]:
    values_file = tmp_path / "environments" / "dev" / "values.yaml"
    values_file.parent.mkdir(parents=True)
    values_file.write_text(values, encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "ENV_REPO_PATH": str(tmp_path),
            "TARGET_ENV": "dev",
            "IMAGE_TAG": "20260814-0310-3f5882a",
            "SERVICES_CSV": services,
            "SKIP_DB_MIGRATE": "true",
        }
    )
    return subprocess.run(
        ["bash", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_adds_missing_service_image_override(tmp_path: Path) -> None:
    result = run_sync(
        tmp_path,
        'apiGateway:\n  image:\n    repository: api-gateway\n    tag: "old"\n',
        "diagnosisService",
    )

    assert result.returncode == 0, result.stderr
    content = (tmp_path / "environments" / "dev" / "values.yaml").read_text(
        encoding="utf-8"
    )
    assert "diagnosisService:\n" in content
    assert "    repository: diagnosis-service\n" in content
    assert '    tag: "20260814-0310-3f5882a"\n' in content


def test_updates_existing_service_tag_without_touching_repository(tmp_path: Path) -> None:
    result = run_sync(
        tmp_path,
        'diagnosisService:\n  image:\n    repository: custom-diagnosis\n    tag: "old"\n',
        "diagnosisService",
    )

    assert result.returncode == 0, result.stderr
    content = (tmp_path / "environments" / "dev" / "values.yaml").read_text(
        encoding="utf-8"
    )
    assert "    repository: custom-diagnosis\n" in content
    assert '    tag: "20260814-0310-3f5882a"\n' in content


def test_rejects_unknown_service(tmp_path: Path) -> None:
    result = run_sync(tmp_path, "global:\n  imageRegistry: example.invalid\n", "unknownService")

    assert result.returncode != 0
    assert "未知服务 key：unknownService" in result.stderr


def test_rejects_existing_service_without_image_tag(tmp_path: Path) -> None:
    result = run_sync(
        tmp_path,
        "diagnosisService:\n  enabled: false\n",
        "diagnosisService",
    )

    assert result.returncode != 0
    assert "diagnosisService 已存在，但缺少可更新的 image.tag" in result.stderr
