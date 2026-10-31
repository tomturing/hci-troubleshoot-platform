"""check_pdb_coverage 的回归测试。

为什么这个门禁自己必须有测试：它的全部价值在于「双向都拦」。2026-10-30 的真实
事故是——源码里 `pdb.yaml` 看起来覆盖了 6 个服务，渲染结果里一个 PDB 都没有
（条件全是 `gt replicaCount 1` 而现网副本数全为 1）。一个只会输出「通过」的
检查器跟没有检查器等价，所以这里对四条判定分支逐一造负例，并特别覆盖两个
**只会让检查变静默**的方向：

  1) 空 selector 的 PDB：K8s 语义是命中全部 Pod，若把它当成「这个服务已被
     PDB 选中」，缺 PDB 的判定就会假通过；
  2) `spec.replicas` 缺省：K8s 默认值是 1，若按 0 处理，「单副本不得有 PDB」
     这条就会对真正需要它的场景沉默。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve().parent / "check_pdb_coverage.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_pdb_coverage", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _deployment(name: str, labels: dict, replicas: int | None = None) -> dict:
    wl = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {"template": {"metadata": {"labels": labels}}},
    }
    # replicas 为 None 时**完全不写这个键**，用来验证「缺省即 1」的口径
    if replicas is not None:
        wl["spec"]["replicas"] = replicas
    return wl


def _pdb(name: str, match_labels: dict) -> dict:
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": name},
        "spec": {"selector": {"matchLabels": match_labels}, "minAvailable": 1},
    }


def _write(tmp_path: Path, docs: list[dict]) -> Path:
    p = tmp_path / "rendered.yaml"
    p.write_text(yaml.safe_dump_all(docs), encoding="utf-8")
    return p


def test_scaled_workload_with_own_pdb_is_clean(tmp_path: Path):
    """正例：副本≥2 且 PDB 的 selector 是 Pod 标签的子集 → 无任何问题。"""
    mod = _load()
    f = _write(
        tmp_path,
        [
            _deployment("case-service", {"app": "case-service", "tier": "backend"}, 2),
            _pdb("case-service-pdb", {"app": "case-service"}),
        ],
    )
    assert mod.check(f) == []


def test_scaled_workload_without_pdb_is_reported(tmp_path: Path):
    """副本≥2 却没有 PDB 选中它：必须报「缺 PDB」，这是节点驱逐路径的实测损伤来源。"""
    mod = _load()
    f = _write(tmp_path, [_deployment("case-service", {"app": "case-service"}, 3)])
    problems = mod.check(f)
    assert len(problems) == 1
    assert "PodDisruptionBudget" in problems[0]


def test_single_replica_with_pdb_is_reported(tmp_path: Path):
    """反向：单副本 + minAvailable=1 会让 kubectl drain 永久卡住，报错方向必须是「多 PDB」。"""
    mod = _load()
    f = _write(
        tmp_path,
        [
            _deployment("api-gateway", {"app": "api-gateway"}, 1),
            _pdb("api-gateway-pdb", {"app": "api-gateway"}),
        ],
    )
    problems = mod.check(f)
    assert any("drain" in p for p in problems), problems


def test_missing_replicas_means_one_not_zero(tmp_path: Path):
    """`spec.replicas` 缺省是 K8s 的 1，不能当 0：否则「单副本不得有 PDB」这条会静默。"""
    mod = _load()
    f = _write(
        tmp_path,
        [
            _deployment("kb-service", {"app": "kb-service"}, None),
            _pdb("kb-service-pdb", {"app": "kb-service"}),
        ],
    )
    problems = mod.check(f)
    assert any("drain" in p for p in problems), problems


def test_empty_selector_pdb_does_not_count_as_protection(tmp_path: Path):
    """空 selector 在 K8s 里命中全部 Pod；若把它当「已保护」，缺 PDB 的判定就会假通过。"""
    mod = _load()
    f = _write(
        tmp_path,
        [
            _deployment("case-service", {"app": "case-service"}, 2),
            _pdb("wide-pdb", {}),
        ],
    )
    problems = mod.check(f)
    assert any("缺" in p or "没有 PodDisruptionBudget" in p for p in problems), problems
    assert any("命中全部 Pod" in p for p in problems), problems


def test_one_pdb_matching_two_deployments_is_reported(tmp_path: Path):
    """一个 PDB 命中多个 Deployment：两个工作负载共用驱逐预算，会被悄悄放大。

    这条负例是「写文档时声明了、但代码只实现了一半」的那个方向：
    原来的 `len(hits) > 1` 查的是「一个 Deployment 被几个 PDB 命中」，方向相反。
    """
    mod = _load()
    f = _write(
        tmp_path,
        [
            _deployment("admin-ui", {"app": "ui", "env": "staging"}, 2),
            _deployment("customer-ui", {"app": "ui", "env": "prod"}, 2),
            _pdb("ui-pdb", {"app": "ui"}),
        ],
    )
    problems = mod.check(f)
    assert any("命中多个 Deployment" in p for p in problems), problems


def test_deployment_hit_by_two_pdbs_is_reported(tmp_path: Path):
    """反方向：一个 Deployment 被两个 PDB 同时命中，驱逐预算的判据不唯一。"""
    mod = _load()
    f = _write(
        tmp_path,
        [
            _deployment("case-service", {"app": "case-service"}, 2),
            _pdb("case-service-pdb", {"app": "case-service"}),
            _pdb("case-service-pdb-old", {"app": "case-service"}),
        ],
    )
    problems = mod.check(f)
    assert any("被多个 PDB 同时命中" in p for p in problems), problems


def test_orphan_pdb_is_reported(tmp_path: Path):
    """selector 命中 0 个工作负载的孤儿 PDB：ArgoCD 会把它同步进去但永远不生效。"""
    mod = _load()
    f = _write(
        tmp_path,
        [
            _deployment("case-service", {"app": "case-service"}, 2),
            _pdb("case-service-pdb", {"app": "gone-service"}),
        ],
    )
    problems = mod.check(f)
    assert any("孤儿 PDB" in p for p in problems), problems


def test_render_without_any_deployment_is_not_silently_ok(tmp_path: Path):
    """喂错文件（比如 obs chart 的渲染结果）时必须报错，不能「0 个对象 = 全绿」。"""
    mod = _load()
    f = _write(
        tmp_path,
        [{"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "postgres"}}],
    )
    problems = mod.check(f)
    assert problems and "没有任何" in problems[0]


def test_main_returns_nonzero_for_missing_file(tmp_path: Path):
    """CLI 层：文件不存在不得返回 0（CI 里 `set -e` 依赖这个退出码）。"""
    mod = _load()
    rc = mod.main(["check_pdb_coverage.py", str(tmp_path / "nope.yaml")])
    assert rc == 1
