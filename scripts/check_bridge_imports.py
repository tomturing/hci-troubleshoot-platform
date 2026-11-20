#!/usr/bin/env python3
"""terminal_bridge 架构 import 禁令扫描（R0c，设计文档 §3.3 硬禁令）。

用法：``python3 scripts/check_bridge_imports.py``

强制规则（违规即 CI 失败）：
1. shared/* 禁止 import 任何功能域、sshx、app、cmd（原语不认识上层）；
2. remote 与 uplink 互不 import（两张脸各自独立）；
3. reconcile 禁止 import delivery/logcollect（骨架不认识消费者，含于规则1）；
4. sshx/app 禁止反向依赖四个业务域 acli/logcollect/delivery/sftp。

冻结基线（只减不增）：
5. 功能域之间的既有互 import 是搬迁前遗留（解环属逻辑收口 S1-S4/uplink 阶段），
   以 BASELINE_DOMAIN_EDGES 冻结现状：新增跨域边直接失败；删除后请同步收紧基线。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "terminal_bridge"
BASE = "terminal_bridge/internal/"
DOMAINS = ["acli", "logcollect", "delivery", "sftp", "terminal", "logpush"]
BUSINESS = ["acli", "logcollect", "delivery", "sftp"]

# R0 搬迁完成时的域间依赖现状（含测试）；逻辑收口阶段逐步清零。
BASELINE_DOMAIN_EDGES = {
    ("acli", "logcollect"), ("acli", "logpush"), ("acli", "terminal"),
    ("logcollect", "logpush"), ("logcollect", "terminal"),
    ("delivery", "logcollect"), ("delivery", "logpush"), ("delivery", "terminal"),
    ("sftp", "logcollect"), ("sftp", "logpush"),
    ("terminal", "logpush"),
}

IMP_RE = re.compile(r'"(terminal_bridge/[^"]+)"')


def imports_of(p: Path) -> list[str]:
    return IMP_RE.findall(p.read_text(encoding="utf-8"))


def main() -> int:
    violations: list[str] = []
    current_edges: set[tuple[str, str]] = set()

    for p in sorted(ROOT.rglob("*.go")):
        rel = p.relative_to(ROOT).as_posix()
        imps = imports_of(p)
        # 规则1：shared 原语不认识上层
        if rel.startswith("internal/shared/"):
            for imp in imps:
                if imp.startswith(BASE) and imp[len(BASE):].split("/")[0] in DOMAINS + ["sshx", "app"]:
                    violations.append(f"[禁令1] {rel} -> {imp}")
                if imp.startswith("terminal_bridge/cmd/"):
                    violations.append(f"[禁令1] {rel} -> {imp}")
        # 规则2：两张脸互不认识
        if rel.startswith("internal/shared/remote/") and any("shared/uplink" in i for i in imps):
            violations.append(f"[禁令2] {rel} import uplink")
        if rel.startswith("internal/shared/uplink/") and any(i.endswith("shared/remote") for i in imps):
            violations.append(f"[禁令2] {rel} import remote")
        # 规则4：sshx/app 不得反向依赖业务域
        if rel.startswith(("internal/sshx/", "internal/app/")):
            for imp in imps:
                if imp in [BASE + d for d in BUSINESS]:
                    violations.append(f"[禁令4] {rel} -> {imp}")
        # 规则5：域间边收集
        for d in DOMAINS:
            if rel.startswith(f"internal/{d}/"):
                for imp in imps:
                    for o in DOMAINS:
                        if o != d and imp == BASE + o:
                            current_edges.add((d, o))

    new_edges = current_edges - BASELINE_DOMAIN_EDGES
    for d, o in sorted(new_edges):
        violations.append(f"[禁令5-新增跨域依赖] {d} -> {o}（跨域协作经 app 装配或 shared 原语）")
    removed = BASELINE_DOMAIN_EDGES - current_edges
    if removed:
        print("[info] 以下跨域依赖已消除，请收紧 BASELINE_DOMAIN_EDGES:", sorted(removed))

    if violations:
        print("import 禁令扫描失败:")
        for v in violations:
            print(" ", v)
        return 1
    print(f"import 禁令扫描通过（域间既有依赖 {len(current_edges)} 条，未新增）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
