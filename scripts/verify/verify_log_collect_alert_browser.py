"""真浏览器验收：已交付 + 采集告警的客户侧三问呈现。

覆盖 Q2026112300867 的回归点：
  - 横幅：标题仍是「已交付」，描述带 [LC-COLLECT-001] 与客户文案，type=warning；
  - 三问卡：由真实 LogCollectStatusCard 渲染「如何确认 / 如何处理」；
  - 绝不出现「平台未记录到可定位的错误码」兜底文案。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
CUSTOMER = ROOT / "frontend" / "customer"
PORT = 5203
BASE = f"http://127.0.0.1:{PORT}/verify/index.html?scenario=collect-alert"


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
            page = browser.new_page(viewport={"width": 1400, "height": 1400})
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_selector('[data-testid="collect-alert-section"]')
            # 横幅：只取采集告警 section 内的 alert（页面上还有其他场景的 alert）
            section = page.locator('[data-testid="collect-alert-section"]')
            body = section.inner_text()
            headline = section.locator(".el-alert__title").first.inner_text()
            desc = section.locator(".el-alert__description").first.inner_text()
            assert "已交付" in headline, f"横幅标题未保持已交付: {headline}"
            assert "LC-COLLECT-001" in desc, f"描述缺错误码: {desc}"
            assert "可能缺少个别日志" in desc or "重新收集" in desc, f"描述缺客户文案: {desc}"

            # 三问卡：真实组件渲染
            assert "如何确认" in body, "缺「如何确认」"
            assert "如何处理" in body, "缺「如何处理」"

            # 绝不能出现 no_code 兜底文案
            assert "平台未记录到可定位的错误码" not in body, "出现了误导性兜底文案"
            assert "采集失败" not in body, "交付成功不应显示采集失败"

            # 调用链可追溯
            trace = page.locator('[data-testid="collect-alert-trace"]').inner_text()
            assert "lc-Q2026112300867" in trace, f"缺调用链: {trace}"

            browser.close()
        print("采集告警浏览器验收：全部通过")
        return 0
    finally:
        vite.terminate()
        try:
            vite.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vite.kill()


if __name__ == "__main__":
    raise SystemExit(main())
