#!/usr/bin/env python3
"""preStop 覆盖率门禁：防止「发布瞬间打到已停止监听的旧 Pod」再次回归。

背景（2026-10-30 staging 实测）：
  13 个 Deployment 的 lifecycle.preStop 全部为空。旧 Pod 收到 SIGTERM 是毫秒级
  停止监听，而它从 EndpointSlice 消失、再经 Traefik / kube-proxy 传播到每条转发
  路径需要约 2s —— 这个差集就是发布时的 502 窗口（case-service 经 Traefik 施压
  2338 次吃到 28 个，同期 endpoints 全程非空）。
  修复是共享 helper hci.preStopLifecycle + 每个 Deployment 主动 include。

两道检查缺一不可：
  1) 渲染结果：真正会跑起来的 Pod 模板必须带 preStop（行为面）；
  2) 模板源码：enabled=false 的服务渲染不出来，只能查源码有没有 include
     （否则新加服务很容易漏掉，且 CI 看不出来）。

用法：
  python3 scripts/ci/check_prestop_coverage.py /tmp/hci-platform-rendered.yaml
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TEMPLATES_DIR = Path("deploy/helm/hci-platform/templates")
# 观测栈由独立 chart 维护（多为数据库/带锁存储，发布策略另行判断），不属于本门禁范围
EXCLUDE_DIRS = ("observability/",)
HELPER_CALL = "hci.preStopLifecycle"


def _split_docs(text: str) -> list[str]:
    """按 YAML 文档分隔符切分 helm template 的输出。"""
    return [d for d in re.split(r"(?m)^---\s*$", text) if d.strip()]


def check_rendered(path: Path) -> list[str]:
    """渲染结果里每个 Deployment 都必须带 preStop。"""
    problems = []
    seen = 0
    for doc in _split_docs(path.read_text(encoding="utf-8")):
        if not re.search(r"(?m)^kind: Deployment$", doc):
            continue
        seen += 1
        match = re.search(r"(?m)^  name: (\S+)$", doc)
        name = match.group(1) if match else "<未知>"
        spec = doc[doc.find("spec:") :] if "spec:" in doc else doc
        if not re.search(r"(?m)^\s+preStop:$", spec):
            problems.append(f"渲染结果里的 Deployment {name} 没有 lifecycle.preStop")
        elif not re.search(r"sleep \d+", spec):
            problems.append(f"渲染结果里的 Deployment {name} 的 preStop 不含 sleep")
    if seen == 0:
        problems.append("渲染结果里没有任何 Deployment，检查输入文件是否为空")
    return problems


def check_templates(root: Path = TEMPLATES_DIR) -> list[str]:
    """源码层：会渲染出 Deployment 的模板必须 include 共享 helper。"""
    problems = []
    for path in sorted(root.rglob("*.yaml")):
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(d) for d in EXCLUDE_DIRS):
            continue
        text = path.read_text(encoding="utf-8")
        if not re.search(r"(?m)^kind: Deployment$", text):
            continue
        if HELPER_CALL not in text:
            problems.append(f"模板 {path} 渲染 Deployment 但没有 include {HELPER_CALL}")
    return problems


def main(argv: list[str]) -> int:
    problems = check_templates()

    if len(argv) > 1:
        rendered = Path(argv[1])
        if not rendered.is_file() or rendered.stat().st_size == 0:
            problems.append(f"渲染结果文件不存在或为空：{rendered}")
        else:
            problems.extend(check_rendered(rendered))

    if problems:
        for p in problems:
            print(f"::error::{p}")
        return 1

    print("preStop 覆盖率检查通过：模板源码与渲染结果均覆盖")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
