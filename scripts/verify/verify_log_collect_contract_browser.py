#!/usr/bin/env python3
"""真浏览器运行时校验：契约生成物在 chromium 里的口径与后端一致。

为什么需要：vitest 用 happy-dom，只能证明组件在模拟环境里渲染出某段文字；
codegen 产物是 TS，真正跑在浏览器里的是打包后的 JS。本脚本把真实生成物
用 esbuild 打成 IIFE 注入 chromium，直接调用 classifyFault / formatFaultText /
UI_TEXT，验证「同一份契约在浏览器里没有走样」。

用法（需 playwright 与 chromium，以及前端已安装依赖）：
    uv run --no-project --with playwright python scripts/verify/verify_log_collect_contract_browser.py

退出码：0=通过，1=口径不一致，2=环境不具备（未验证，不等于通过）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "frontend" / "shared" / "src"
FRONTEND_CWD = ROOT / "frontend" / "customer"
# esbuild 是 vite 的间接依赖，不会链进应用的 .bin；从 pnpm 存储里定位原生二进制。
ESBUILD_BINARIES = sorted(
    (ROOT / "frontend" / "node_modules" / ".pnpm").glob(
        "@esbuild+linux-x64@*/node_modules/@esbuild/linux-x64/bin/esbuild"
    )
)

SPC_MESSAGE = (
    "场景清单写入未通过（目标 1 台，成功 0 台）：172.28.24.2 — 场景清单目录缺失；"
    "回滚失败 1 台，这些节点可能残留平台清单（重试前须人工核对）"
)

# 每项都是「浏览器里真实执行」的表达式，结果必须是 true。
ASSERTIONS = [
    ("运行时码已并入浏览器产物", "LC.FAULT_CATALOG['LC-SPC-001'] !== undefined"),
    ("SPC 文案不再落到未归因兜底码", f"LC.classifyFault({json.dumps(SPC_MESSAGE, ensure_ascii=False)}) !== 'LC-HTP-002'"),
    ("权威码的客户文案可定界", "LC.formatFaultText('LC-SPC-001').includes('场景清单目录缺失')"),
    ("聚合码不再出现错误量词", "!LC.FAULT_CATALOG['LC-SPC-010'].problem.includes('多台节点')"),
    ("兜底文案不再谎称平台没给码", "!LC.UI_TEXT.fallback.no_code.includes('平台未给出可识别的错误码')"),
    ("同视图原文只渲染一次", "LC.RENDER_POLICY.error_message_max_per_view === 1"),
    ("以 error_code 为归因权威", "LC.RENDER_POLICY.error_code_authoritative === true"),
    ("无数据四态文案齐备", "['not_reached','bridge_outdated','report_failed','genuinely_empty'].every(k => !!LC.MISSING_DATA_TEXT[k])"),
    ("唯一调用链契约可读", "LC.OBSERVABILITY_CONTRACT.trace.header === 'X-Trace-ID'"),
]


def build_bundle(workdir: Path) -> Path:
    entry = workdir / "entry.ts"
    entry.write_text(
        f"export * from '{SHARED}/logCollectDisplay'\n"
        f"export * from '{SHARED}/logCollectUi'\n",
        encoding="utf-8",
    )
    out = workdir / "lc-shared.js"
    if not ESBUILD_BINARIES:
        raise RuntimeError("未找到 esbuild 原生二进制，请先执行 pnpm install")
    esbuild = str(ESBUILD_BINARIES[-1])
    result = subprocess.run(
        [esbuild, "--bundle", "--format=iife", "--global-name=LC",
         str(entry), f"--outfile={out}"],
        cwd=FRONTEND_CWD, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"esbuild 失败：{result.stderr[-800:]}")
    return out


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 未安装 playwright Python 包", file=sys.stderr)
        return 2

    with TemporaryDirectory() as tmp:
        bundle = build_bundle(Path(tmp))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content("<!doctype html><html><body></body></html>")
            page.add_script_tag(content=bundle.read_text(encoding="utf-8"))
            failed = []
            for name, expr in ASSERTIONS:
                ok = page.evaluate(f"() => Boolean({expr})")
                print(("ok   " if ok else "FAIL ") + name)
                if not ok:
                    failed.append(name)
            print(f"浏览器产物码数：{page.evaluate('() => Object.keys(LC.FAULT_CATALOG).length')}")
            browser.close()
    if failed:
        for name in failed:
            print("FAIL:", name, file=sys.stderr)
        return 1
    print(f"[PASS] 契约生成物在 chromium 中 {len(ASSERTIONS)} 项口径一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
