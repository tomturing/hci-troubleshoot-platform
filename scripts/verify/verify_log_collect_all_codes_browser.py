#!/usr/bin/env python3
"""真浏览器逐码验收：契约里每个日志收集错误码都要在客户侧真实页面可解释。

## 为什么需要

PR #1499 的验收只截了契约中 20 个 signal 码的页面，SPC / PLUGIN / EXEC / ENV /
PLATFORM / TRANSFER 共 71 个运行时码从未进过真浏览器。契约升到 v3 后，
``frontend/customer/verify/VerifyApp.vue`` 的字典表已是全码，但「字典里有」
不等于「页面能正确定界并给出建议」。本脚本逐码打开真实 Vue 页面做断言。

## 用法

    uv run --no-project --with playwright python scripts/verify/verify_log_collect_all_codes_browser.py
    uv run --no-project --with playwright python scripts/verify/verify_log_collect_all_codes_browser.py --screenshots /tmp/lc-codes
    uv run --no-project --with playwright python scripts/verify/verify_log_collect_all_codes_browser.py --limit 5

退出码：0=全码通过，1=有码不达标，2=环境不具备（未验证，不等于通过）。
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "log_collect_display.json"
FRONTEND = ROOT / "frontend" / "customer"
PORT = 5199
BASE = f"http://127.0.0.1:{PORT}"

# 旧兜底文案：契约 v3 之后任何码都不该再显示这句（明明有码却说平台没给码）。
FORBIDDEN_TEXT = "平台未给出可识别的错误码"


def wait_for_port(timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def run(limit: int, screenshots: Path | None) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 未安装 playwright Python 包", file=sys.stderr)
        return 2

    codes = sorted(json.loads(CONTRACT.read_text(encoding="utf-8"))["fault_catalog"].keys())
    if limit:
        codes = codes[:limit]

    server = subprocess.Popen(
        ["pnpm", "exec", "vite", "--host", "127.0.0.1", "--port", str(PORT), "--strictPort"],
        cwd=FRONTEND,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_port():
            print("[SKIP] Vite 未能在 40s 内监听端口", file=sys.stderr)
            return 2
        if screenshots:
            screenshots.mkdir(parents=True, exist_ok=True)

        failures: list[str] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            total_rows = None
            for code in codes:
                # Vite 的 HMR websocket 会让 networkidle 永不触发，改用 DOM 就绪 + 选择器。
                page.goto(f"{BASE}/verify/index.html?code={code}", wait_until="domcontentloaded")
                page.wait_for_selector(".verify-header")
                header = page.locator(".verify-header").inner_text()
                inline = page.locator(".verify-inline-text").inner_text()
                body = page.locator("body").inner_text()

                problems = []
                if code not in header:
                    problems.append("场景标题未显示该码")
                if not inline.strip():
                    problems.append("客户可解释文案为空")
                if FORBIDDEN_TEXT in body:
                    problems.append("仍出现旧的错误兜底文案")
                if total_rows is None:
                    # 字典标题由 VerifyApp.vue 直接渲染 catalogRows.length，
                    # 比依赖 Element Plus 表格 DOM 结构更稳。
                    title = page.locator("h2", has_text="错误字典全量").inner_text()
                    match = re.search(r"（(\d+) 条", title)
                    total_rows = int(match.group(1)) if match else 0
                    expected = len(json.loads(CONTRACT.read_text(encoding="utf-8"))["fault_catalog"])
                    if total_rows != expected:
                        problems.append(f"字典表条数 {total_rows} != 契约码数 {expected}")

                if problems:
                    failures.append(f"{code}: {'；'.join(problems)}")
                elif screenshots:
                    page.screenshot(path=str(screenshots / f"{code}.png"), full_page=True)
            browser.close()

        print(f"逐码验收：{len(codes)} 个码，字典表行数 {total_rows}，失败 {len(failures)}")
        for item in failures:
            print("FAIL:", item, file=sys.stderr)
        return 1 if failures else 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只验前 N 个码，便于快速冒烟")
    parser.add_argument("--screenshots", type=Path, default=None, help="逐码全页截图输出目录")
    args = parser.parse_args()
    return run(args.limit, args.screenshots)


if __name__ == "__main__":
    sys.exit(main())
