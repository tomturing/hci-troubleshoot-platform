#!/usr/bin/env python3
"""校验 ArgoCD Application 清单的同步不变式。

规则 3（见 docs/deploy/部署设计.md §9.6）：
    带 automated.selfHeal 的 Application 必须显式声明 syncPolicy.retry（limit >= 3）。

原因：selfHeal 只负责"恢复漂移"，不会重试失败的操作。当迁移 hook Job 因依赖对象缺失、
API 抖动等瞬态原因失败时，没有 retry 的 Application 会停在失败态等人手工介入；
有 retry 时下一个退避窗口会自动重跑，并在重跑过程中重建依赖（wave -10/-9 先于 Job）。
显式声明还能避免"依赖 ArgoCD 版本默认重试次数"这种隐式契约。

用法：
    python3 scripts/ci/check_argocd_app_sync_invariants.py [--path deploy/gitops/argo-apps]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

MIN_RETRY_LIMIT = 3
DEFAULT_PATH = "deploy/gitops/argo-apps"


def iter_applications(root: pathlib.Path):
    """遍历目录下所有 YAML 中的 ArgoCD Application 文档。"""
    for path in sorted(root.rglob("*.yaml")):
        try:
            documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        except yaml.YAMLError as exc:  # 解析失败本身也是错误，交由调用方汇总
            yield path, None, f"YAML 解析失败: {exc}"
            continue
        for doc in documents:
            if not isinstance(doc, dict) or doc.get("kind") != "Application":
                continue
            yield path, doc, None


def collect_violations(root: pathlib.Path) -> list[str]:
    violations: list[str] = []
    for path, doc, error in iter_applications(root):
        if error is not None:
            violations.append(f"{path}: {error}")
            continue
        spec = doc.get("spec") or {}
        sync_policy = spec.get("syncPolicy") or {}
        automated = sync_policy.get("automated") or {}
        if automated.get("selfHeal") is not True:
            continue
        name = (doc.get("metadata") or {}).get("name", "<unnamed>")
        retry = sync_policy.get("retry") or {}
        limit = retry.get("limit")
        if not isinstance(limit, int) or limit < MIN_RETRY_LIMIT:
            violations.append(
                f"{path}: Application {name} 启用了 automated.selfHeal，"
                f"但缺少 syncPolicy.retry.limit >= {MIN_RETRY_LIMIT}（当前 {limit!r}）"
            )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PATH, help="Application 清单目录")
    args = parser.parse_args()

    root = pathlib.Path(args.path)
    if not root.is_dir():
        print(f"目录不存在: {root}", file=sys.stderr)
        return 1

    violations = collect_violations(root)
    if violations:
        print("ArgoCD Application 同步不变式校验失败：", file=sys.stderr)
        for item in violations:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\n修复方式：在该 Application 的 spec.syncPolicy 下补充\n"
            "  retry:\n"
            "    limit: 5\n"
            "    backoff:\n"
            "      duration: 15s\n"
            "      factor: 2\n"
            "      maxDuration: 2m",
            file=sys.stderr,
        )
        return 1

    print(f"ArgoCD Application 同步不变式校验通过（{root}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
