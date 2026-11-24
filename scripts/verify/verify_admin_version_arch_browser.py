#!/usr/bin/env python3
"""真浏览器验收：管理端任务进展「版本架构」/「acli版本」展示。

验收页与产品详情调用同一个 @hci/shared 拼接函数；本脚本在 chromium 里
逐项检查表格标签与取值，证明两端口径一致。

用法（需 playwright 与 chromium，以及前端已安装依赖）：
    uv run --no-project --with playwright python scripts/verify/verify_admin_version_arch_browser.py

退出码：0=通过，1=断言失败，2=环境不具备。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ADMIN = ROOT / "frontend" / "admin"
PORT = 5216
# admin 的 vite base 是 /admin/，verify 页必须带这个前缀
BASE = f"http://127.0.0.1:{PORT}/admin/verify/index.html?scenario=version-arch"


def main() -> int:
    env = os.environ.copy()
    env.update({"NO_COLOR": "1", "FORCE_COLOR": "0"})
    vite = subprocess.Popen(
        ["pnpm", "exec", "vite", "--host", "127.0.0.1", "--port", str(PORT), "--strictPort"],
        cwd=ADMIN,
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
            section = page.locator('[data-testid="version-arch-section"]')
            section.wait_for()
            labels = section.locator(".el-descriptions__label").all_inner_texts()
            values = section.locator(".el-descriptions__content").all_inner_texts()
            assert labels == ["版本架构", "acli版本"], f"表格标签不符: {labels}"
            assert values == [
                "HCI 6.10.0_R2 / x86_64",
                "1.0.1 (build 2026-09-20 17:22:30)",
            ], f"表格取值不符: {values}"
            browser.close()
        print("管理端任务进展版本架构浏览器验收：全部通过")
        return 0
    finally:
        vite.terminate()
        try:
            vite.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vite.kill()


if __name__ == "__main__":
    raise SystemExit(main())
