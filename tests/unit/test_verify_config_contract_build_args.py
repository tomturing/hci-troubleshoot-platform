"""配置契约：`compose_build_args`（构建期参数）类别的门禁守护。

背景（避坑 D-028）：`Dockerfile.migrations` 的 `MIRROR_MODE` 只在镜像构建期生效，
不是容器运行时环境变量。它出现在 compose 的 `build.args` 里会被 `COMPOSE_REFERENCE`
当成运行时变量，从而要求 `.env.example` + Helm 等价映射——给构建期参数建模独立类别
后，才能既让 CI 显式传 `MIRROR_MODE=off`，又不污染运行时配置清单。

测试用「复制脚本到临时树」的方式：脚本按 `Path(__file__).parents[2]` 定位仓库根，
复制后临时目录即成为独立仓库根，无需真实仓内文件参与。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify" / "verify_config_contract.py"

CONTRACT_TEMPLATE = """\
version: 1
sources:
  env_example: .env.example
  compose: deploy/docker/docker-compose.yml
  helm_values: deploy/helm/hci-platform/values.yaml
  helm_templates: deploy/helm/hci-platform/templates
non_compose: []
helm_equivalents: {{}}
helm_only_runtime: []
{extra}
"""


def _build_repo(tmp_path: Path, contract_extra: str) -> Path:
    """搭一个最小可校验仓库树，返回脚本路径。"""
    (tmp_path / "scripts" / "verify").mkdir(parents=True)
    shutil.copy(SCRIPT, tmp_path / "scripts" / "verify" / "verify_config_contract.py")

    (tmp_path / ".env.example").write_text("# 本测试不涉及运行时变量\n", encoding="utf-8")

    compose_dir = tmp_path / "deploy" / "docker"
    compose_dir.mkdir(parents=True)
    (compose_dir / "docker-compose.yml").write_text(
        textwrap.dedent(
            """\
            services:
              db-migrate:
                build:
                  args:
                    MIRROR_MODE: ${MIRROR_MODE:-on}
            """
        ),
        encoding="utf-8",
    )

    helm_dir = tmp_path / "deploy" / "helm" / "hci-platform"
    (helm_dir / "templates").mkdir(parents=True)
    (helm_dir / "values.yaml").write_text("{}\n", encoding="utf-8")
    (helm_dir / "templates" / "noop.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: noop\n", encoding="utf-8"
    )

    contract_dir = tmp_path / "deploy" / "config"
    contract_dir.mkdir(parents=True)
    (contract_dir / "config-contract.yaml").write_text(
        CONTRACT_TEMPLATE.format(extra=contract_extra), encoding="utf-8"
    )
    return tmp_path / "scripts" / "verify" / "verify_config_contract.py"


def _run(script: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), "--skip-render"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_declared_build_arg_passes(tmp_path: Path) -> None:
    """登记为 compose_build_args 后，构建期参数不再要求 .env.example/Helm 登记。"""
    script = _build_repo(tmp_path, "compose_build_args:\n  - MIRROR_MODE\n")
    result = _run(script, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "配置契约校验通过" in result.stdout


def test_undeclared_build_arg_fails_loud(tmp_path: Path) -> None:
    """未登记时保持原有严格性：仍按「Compose 引用但 .env.example 未声明」拒绝。"""
    script = _build_repo(tmp_path, "")
    result = _run(script, tmp_path)
    assert result.returncode == 1
    assert "Compose 引用但 .env.example 未声明" in result.stdout
    assert "MIRROR_MODE" in result.stdout


def test_build_arg_cannot_be_helm_equivalent(tmp_path: Path) -> None:
    """构建期参数与运行时/Helm 配置互斥，混登即拒（防止语义腐化）。"""
    script = _build_repo(
        tmp_path,
        "compose_build_args:\n  - MIRROR_MODE\nhelm_equivalents:\n  MIRROR_MODE:\n    value_paths: []\n",
    )
    result = _run(script, tmp_path)
    assert result.returncode == 1
    assert "compose_build_args 不应同时登记为运行时/Helm 配置" in result.stdout


def test_stray_build_arg_detected(tmp_path: Path) -> None:
    """契约声明了 compose 里并不存在的构建期参数，必须报错而不是静默放行。"""
    script = _build_repo(tmp_path, "compose_build_args:\n  - NOT_IN_COMPOSE\n")
    result = _run(script, tmp_path)
    assert result.returncode == 1
    assert "契约 compose_build_args 未被 Compose 引用" in result.stdout


@pytest.mark.parametrize("raw_line", ["MIRROR_MODE: ${MIRROR_MODE:-on}\n"])
def test_real_compose_file_declares_mirror_mode(tmp_path: Path, raw_line: str) -> None:
    """真仓守护：compose 的 db-migrate 必须显式声明 MIRROR_MODE build arg。

    直接读真仓文件而非临时树——这是 D-028 的回归钉子，防止有人把
    `build.args.MIRROR_MODE` 删掉后 CI 又退回 ARG 默认值（阿里云源）。
    """
    compose_text = (REPO_ROOT / "deploy" / "docker" / "docker-compose.yml").read_text(encoding="utf-8")
    assert raw_line in compose_text, "compose 的 db-migrate 缺少 MIRROR_MODE build arg"
    contract_text = (REPO_ROOT / "deploy" / "config" / "config-contract.yaml").read_text(encoding="utf-8")
    assert "compose_build_args" in contract_text and "MIRROR_MODE" in contract_text
