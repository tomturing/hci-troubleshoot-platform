#!/usr/bin/env python3
"""真浏览器验收：日志收集探测卡「版本架构」/「acli版本」展示。

产品组件与验收页共用 formatVersionArchitecture，本脚本在 chromium 里逐项检查
标签、取值与降级边界，避免 happy-dom 单测覆盖不到的浏览器差异。

用法（需 playwright 与 chromium，以及前端已安装依赖）：
    uv run --no-project --with playwright python scripts/verify/verify_log_collect_version_arch_browser.py

退出码：0=通过，1=断言失败，2=环境不具备。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
CUSTOMER = ROOT / "frontend" / "customer"
PORT = 5204
BASE = f"http://127.0.0.1:{PORT}/verify/index.html?scenario=probe-info"


def main() -> int:
    env = os.environ.copy()
    env.update({"NO_COLOR": "1", "FORCE_COLOR": "0"})
    vite = subprocess.Popen(
        ["pnpm", "exec", "vite", "--host", "127.0.0.1", "--port", str(PORT), "--strictPort"],
        cwd=CUSTOMER,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                import urllib.request

                urllib.request.urlopen(BASE, timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("vite did not become ready")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(BASE, wait_until="networkidle")
            section = page.locator('[data-testid="probe-info-section"]')
            section.wait_for()
            # 只断言表格本身的标签与取值，不把说明文字里的「架构」算进来
            labels = section.locator(".el-descriptions__label").all_inner_texts()
            values = section.locator(".el-descriptions__content").all_inner_texts()
            assert labels == ["版本架构", "acli版本"], f"表格标签不符: {labels}"
            assert values == [
                "HCI 6.10.0_R2 / x86_64",
                "1.0.1 (build 2026-09-20 17:22:30)",
            ], f"表格取值不符: {values}"

            # 降级边界：直接在浏览器里调用产品同一份纯函数
            degraded = page.evaluate(
                """async () => {
                  const module = await import('/src/utils/logCollectProbeDisplay.ts')
                  const fmt = module.formatVersionArchitecture
                  return {
                    missing: fmt('x86_64', undefined),
                    badType: fmt('x86_64', { unexpected: true }),
                    noArch: fmt('', '6.10.0_R2'),
                  }
                }"""
            )
            assert degraded == {
                "missing": "x86_64",
                "badType": "x86_64",
                "noArch": "HCI 6.10.0_R2",
            }, f"降级边界在浏览器里走样: {degraded}"

            browser.close()
        print("探测卡版本架构浏览器验收：全部通过")
        return 0
    finally:
        vite.terminate()
        try:
            vite.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vite.kill()


if __name__ == "__main__":
    raise SystemExit(main())
