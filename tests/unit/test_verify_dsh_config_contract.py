"""DSH Compose/Helm 语义配置契约测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify/verify_dsh_config_contract.py"


def run_contract(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_repository_dsh_contract_covers_offline_compose_variables() -> None:
    """仓库内所有 DSH_OFFLINE_* Compose 输入必须拥有三层映射。"""
    result = run_contract("--skip-render")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "映射=20" in result.stdout


def test_missing_offline_mapping_fails_loudly(tmp_path: Path) -> None:
    """删除一条映射时必须阻断，而不是只提示人工注意。"""
    contract = yaml.safe_load((ROOT / "deploy/config/dsh-config-contract.yaml").read_text(encoding="utf-8"))
    contract["mappings"] = [item for item in contract["mappings"] if item["compose_env"] != "DSH_OFFLINE_LLM_MODEL"]
    contract_path = tmp_path / "dsh-config-contract.yaml"
    contract_path.write_text(yaml.safe_dump(contract, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = run_contract("--skip-render", "--contract", str(contract_path))
    assert result.returncode == 1
    assert "DSH_OFFLINE_LLM_MODEL" in result.stdout


def test_actual_values_compare_without_printing_sensitive_values(tmp_path: Path) -> None:
    """环境仓传入实际值时，值不一致应失败且输出不能泄漏凭据。"""
    (tmp_path / "deploy/docker").mkdir(parents=True)
    (tmp_path / "deploy/helm/hci-platform-dsh/templates").mkdir(parents=True)
    (tmp_path / "deploy/helm/hci-platform-dsh/values.yaml").write_text(
        "dsh:\n  llm:\n    model: default-model\n", encoding="utf-8"
    )
    (tmp_path / "deploy/helm/hci-platform-dsh/templates/env.yaml").write_text(
        "- name: DSH_LLM_MODEL\n", encoding="utf-8"
    )
    (tmp_path / ".env.example").write_text("DSH_OFFLINE_LLM_MODEL=example-model\n", encoding="utf-8")
    (tmp_path / "deploy/docker/docker-compose.yml").write_text(
        "services:\n  dsh-offline:\n    environment:\n      DSH_LLM_MODEL: ${DSH_OFFLINE_LLM_MODEL}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("DSH_OFFLINE_LLM_MODEL=actual-model\n", encoding="utf-8")
    (tmp_path / "dsh-values.yaml").write_text("dsh:\n  llm:\n    model: different-model\n", encoding="utf-8")
    contract = {
        "version": 1,
        "sources": {"env_example": ".env.example", "compose": "deploy/docker/docker-compose.yml"},
        "mappings": [
            {
                "id": "offline.llm_model",
                "compose_env": "DSH_OFFLINE_LLM_MODEL",
                "helm_chart": "deploy/helm/hci-platform-dsh",
                "helm_values": ["dsh.llm.model"],
                "rendered_env": ["DSH_LLM_MODEL"],
            }
        ],
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")

    result = run_contract(
        "--root",
        str(tmp_path),
        "--contract",
        str(contract_path),
        "--compose-env",
        str(tmp_path / ".env"),
        "--dsh-values",
        str(tmp_path / "dsh-values.yaml"),
        "--skip-render",
    )
    assert result.returncode == 1
    assert "DSH_OFFLINE_LLM_MODEL" in result.stdout
    assert "different-model" not in result.stdout
