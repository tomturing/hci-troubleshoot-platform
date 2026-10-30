#!/usr/bin/env python3
"""PDB 覆盖面门禁：比对渲染结果里的 Deployment 副本数与 PDB，双向拦截。

为什么必须查渲染结果而不是查模板源码：
  2026-10-30 staging 现网 PDB 数为 0。pdb.yaml 是手工复制出来的，只覆盖 6/13 个
  服务，而条件全是 `gt replicaCount 1`，staging 全部副本数为 1 —— 源码看上去
  "有 PDB 模板"，实际一个都没渲染出来。只有渲染结果不会说谎。

双向都要拦，单向会漏另一半：
  1) 缺 PDB：replicas≥2 的 Deployment 必须有 PDB 选中它
     —— 否则节点驱逐/维护可以把它打到 0 副本（非发布路径的实测损伤：
     并发删 Pod 产生 503×1971 + 502×187）；
  2) 多 PDB：replicas≤1 的 Deployment 不得有 PDB
     —— minAvailable=1 配单副本会让 `kubectl drain` 永远等不到「还能驱逐」，
     保护直接变成故障（运维上是比缺 PDB 更难排查的现象： drain 卡死不报错）。

另加一条一致性检查：PDB 的 selector 不得命中 0 个或多个 Deployment。命中多个
意味着两个 Deployment 共用一份 minAvailable 预算，驱逐预算会被悄悄放大。

用法：
  python3 scripts/ci/check_pdb_coverage.py <rendered.yaml> [<rendered2.yaml> ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# 只统计这些 kind 的副本数；StatefulSet/DaemonSet 不在 PDB 门禁范围内
# （postgres/redis 有各自的备份与运维流程，观测栈由 obs chart 判断）
SCALED_KINDS = ("Deployment",)


def _docs(path: Path) -> list[dict]:
    out = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, dict) and doc.get("kind"):
            out.append(doc)
    return out


def _matches(pdb_selector: dict, pod_labels: dict) -> bool:
    """PDB 的 matchLabels 是否为 Pod 标签的子集（K8s 语义）。"""
    for key, value in (pdb_selector or {}).items():
        if pod_labels.get(key) != value:
            return False
    return bool(pdb_selector)


def check(path: Path) -> list[str]:
    docs = _docs(path)
    workloads = [d for d in docs if d.get("kind") in SCALED_KINDS]
    pdbs = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
    problems: list[str] = []

    if not workloads:
        return [f"{path}: 渲染结果里没有任何 {SCALED_KINDS}，检查输入是否为空"]

    for wl in workloads:
        name = wl.get("metadata", {}).get("name", "<未知>")
        # spec.replicas 缺省即 1（K8s 默认值），不能当 0 处理
        replicas = int(wl.get("spec", {}).get("replicas", 1) or 0)
        pod_labels = wl.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {}) or {}
        hits = [
            p.get("metadata", {}).get("name", "<未知>")
            for p in pdbs
            if _matches(p.get("spec", {}).get("selector", {}).get("matchLabels", {}), pod_labels)
        ]
        if replicas >= 2 and not hits:
            problems.append(
                f"{name}: replicas={replicas} 但没有 PodDisruptionBudget 选中它"
                f"（pdb.yaml 的 $targets 是否漏加了这个服务？）"
            )
        if replicas < 2 and hits:
            problems.append(
                f"{name}: replicas={replicas} 却存在 PDB {hits}——单副本 + minAvailable≥1 会让 kubectl drain 永久卡住"
            )
        if len(hits) > 1:
            problems.append(f"{name}: 被多个 PDB 同时命中（{hits}），驱逐预算的判据不唯一")

    for p in pdbs:
        pname = p.get("metadata", {}).get("name", "<未知>")
        sel = p.get("spec", {}).get("selector", {}).get("matchLabels", {}) or {}
        hit = [
            wl.get("metadata", {}).get("name", "<未知>")
            for wl in workloads
            if _matches(sel, wl.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {}) or {})
        ]
        if not sel:
            problems.append(f"{pname}: selector.matchLabels 为空，会命中全部 Pod")
        elif not hit:
            problems.append(f"{pname}: selector {sel} 命中 0 个 Deployment，是个孤儿 PDB")
        elif len(hit) > 1:
            # 本函数的文档口径一开始就写了「PDB 的 selector 不得命中 0 个或多个
            # Deployment」，但当时只落了「0 个」这一半：上面 per-workload 的
            # len(hits) > 1 查的是「一个 Deployment 被几个 PDB 命中」，方向相反，
            # 所以「一个 PDB 被两个 Deployment 共用」长期检不出来（新加的
            # scripts/ci/test_check_pdb_coverage.py 首次跑就红了）。两个方向都要查。
            problems.append(
                f"{pname}: selector {sel} 同时命中多个 Deployment（{hit}），"
                f"它们共用一份 minAvailable 预算，驱逐预算会被悄悄放大"
            )

    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法: python3 scripts/ci/check_pdb_coverage.py <rendered.yaml> [...]", file=sys.stderr)
        return 2
    problems: list[str] = []
    for arg in argv[1:]:
        path = Path(arg)
        if not path.is_file():
            problems.append(f"文件不存在: {path}")
            continue
        problems.extend(check(path))
    if problems:
        print("PDB 覆盖面检查未通过：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print(f"✅ PDB 覆盖面检查通过（{len(argv) - 1} 份渲染结果）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
