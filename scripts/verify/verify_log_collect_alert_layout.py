#!/usr/bin/env python3
"""真浏览器几何校验：日志收集面板的提示条不得被 flex 压缩。

## 为什么需要这个脚本

2026-10-27 现场第三次反馈「提示条文字被覆盖/只剩半行」，此前两次修复（#1295 底条分层、
#1299 固定提示区）都没修对，原因不是改得不够，而是**验收指标错了**：

* 真实机制：`.lc-scroll`/`.lc-alerts` 是 `flex column + overflow-y:auto`，内容超出产生负剩余
  空间，按 flex 规则由子项 `flex-shrink` 消化。`el-form` 等子项 `overflow:visible`，
  `min-height:auto` 解析为内容高，压不动；`el-alert` 自带 `overflow:hidden`，自动最小尺寸
  退化为 0 → 负空间全灌到提示条上，64px 被压成 16px，再被 `align-items:center` 上下对半裁。
* 过去的验收断言是「底条与内容几何不重叠（overlap=0）」。flex 压缩下盒子整齐堆叠、
  **永不重叠**，所以断言恒绿——文字是被自己的盒子裁掉的。

因此本脚本只量一件事：每个提示条的 `clientHeight` 是否 ≥ 它自身内容需要的 `scrollHeight`。

## 与单测的关系

`frontend/customer/src/components/__tests__/LogCollectPanel.spec.ts` 里的同名单测只能读源码
断言「守卫规则还在」（happy-dom 无排版引擎，量不出高度），是防退化门铃；真正的几何结论
来自本脚本。

## 用法

    uv tool install playwright && playwright install chromium   # 一次性
    python3 scripts/verify/verify_log_collect_alert_layout.py
    python3 scripts/verify/verify_log_collect_alert_layout.py --screenshot /tmp/panel.png

退出码：0=通过，1=发现提示条被压缩，2=环境不具备（未验证，不等于通过）。

## 夹具的诚实边界

CSS 直接取自 `LogCollectPanel.vue` 的 `<style scoped>` 真源（不是复制），所以守卫规则被删掉
脚本立刻会红；DOM 只复刻「面板骨架 + 真实 el-alert 标记」，取不到业务数据的部分用等价长度的
占位文案。它验证的是**样式契约**，端到端交互仍以现场流程为准。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_VUE = REPO_ROOT / "frontend/customer/src/components/LogCollectPanel.vue"
ELEMENT_CSS = REPO_ROOT / "frontend/customer/node_modules/element-plus/dist/index.css"

# 一行 el-alert 的物理下限：8px padding*2 + 24px line-height = 40px。
# 低于它必然是把文字裁掉了，而不是"设计成紧凑样式"。
MIN_ALERT_HEIGHT = 40


def extract_scoped_style(sfc: str) -> str:
    """取出 SFC 里的 <style scoped> 原文。找不到就 fail-fast，不要静默用空样式。"""
    match = re.search(r"<style scoped>(.*?)</style>", sfc, re.S)
    if not match:
        raise SystemExit(f"[FAIL] {PANEL_VUE} 里找不到 <style scoped>，脚本夹具已失效，请更新脚本")
    return match.group(1)


def build_alert(kind: str, text: str) -> str:
    """复刻 el-alert 的最小真实标记：show-icon + 无 description 时图标为 is-big。"""
    return (
        f'<div class="el-alert el-alert--{kind} is-light" data-alert="1">'
        f'<div class="el-alert__icon is-big">i</div>'
        f'<div class="el-alert__content">'
        f'<span class="el-alert__title">{text}</span>'
        f"</div></div>"
    )


# 现场那条被压扁的原文：接管态提示，两行。文案长度直接影响夹具能否复现。
ALERT_LONG = "以下参数来自平台任务记录（发起节点 172.28.24.193）：任务进行中不可修改，结束后可「重新收集」"
ALERT_SHORT = "请先在 SSH 终端连接 HCI 节点；连接后才能读取场景目录与节点清单"


def build_html(style: str) -> str:
    # 行高写死 56px（inline，不动 SFC 样式）：夹具必须让内容真的超出容器高度，
    # 否则没有负剩余空间，flex 压缩不会发生，脚本就会在"没病"的情况下报通过。
    rows = "".join(
        f'<div style="height:56px">{label}</div>'
        for label in ("故障场景", "时间范围", "发起节点", "目标主机", "日志清理", "备注")
    )
    pinned = "".join(
        [
            build_alert(
                "warning",
                "该工单属于其他会话身份：采集可以继续，但日志回传与终端录像回放会被平台按归属拒绝，建议换用本人名下的工单号。",
            ),
            build_alert("error", "环境准备失败：acli 未安装且自动安装失败，请检查节点网络与 sudo 权限后重试"),
            build_alert("warning", "节点任务进行中：平台记录与节点状态不一致，建议接管继续或取消并重试"),
            build_alert("warning", "清理提醒：采集包仍留在节点 /sf/bin/acli 目录下，请自行清理"),
        ]
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{style}</style></head>
<body>
  <!-- 夹具 A：滚动区（现场红框所在位置）。容器高度刻意小于内容高度，制造负剩余空间 -->
  <div class="lc-panel" style="width: 640px; height: 460px;">
    <div class="lc-scroll" data-fixture="scroll">
      <div class="lc-progress">
        <div class="lc-status">收集任务进行中!（0/1）</div>
      </div>
      {build_alert("info", ALERT_LONG)}
      {build_alert("info", ALERT_SHORT)}
      <div class="fake-form">{rows}</div>
    </div>
    <div class="lc-composer">开始收集</div>
  </div>

  <!-- 夹具 B：固定提示区（45% 上限一旦生效就是负剩余空间） -->
  <div class="lc-panel" style="width: 640px; height: 300px; margin-top: 24px;">
    <div class="lc-alerts" data-fixture="pinned">{pinned}</div>
    <div class="lc-scroll"><div class="fake-form">{rows}</div></div>
  </div>
</body></html>
"""


# 每条提示条都要容纳自身内容；同时给出物理下限，防止"压到刚好半行"也算过。
# 另外回传容器自身的"是否真的溢出"：夹具不溢出时压缩不会发生，结论无效（防空跑）。
MEASURE_JS = """() => {
  const alerts = [...document.querySelectorAll('[data-alert]')].map((el, i) => {
    const box = el.getBoundingClientRect()
    const style = getComputedStyle(el)
    return {
      index: i,
      fixture: el.closest('[data-fixture]')?.dataset.fixture ?? 'unknown',
      height: Math.round(box.height),
      scrollHeight: el.scrollHeight,
      shrink: style.flexShrink,
      text: (el.querySelector('.el-alert__title')?.textContent ?? '').slice(0, 24),
    }
  })
  const containers = [...document.querySelectorAll('[data-fixture]')].map((el) => ({
    fixture: el.dataset.fixture,
    clientHeight: el.clientHeight,
    scrollHeight: el.scrollHeight,
    overflowY: getComputedStyle(el).overflowY,
  }))
  return {alerts, containers}
}"""


def main() -> int:
    parser = argparse.ArgumentParser(description="日志收集面板提示条布局校验")
    parser.add_argument("--screenshot", help="把夹具截图写到该路径，用于人工比对")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 未安装 playwright：uv tool install playwright && playwright install chromium")
        return 2

    if not ELEMENT_CSS.exists():
        print(f"[SKIP] 缺少 {ELEMENT_CSS.relative_to(REPO_ROOT)}：先在 frontend 目录执行 pnpm install")
        print("       （el-alert 的 overflow:hidden 正是压缩的成因之一，没有它测不出真值）")
        return 2

    style = extract_scoped_style(PANEL_VUE.read_text(encoding="utf-8"))
    style = style + "\n" + ELEMENT_CSS.read_text(encoding="utf-8")

    html = build_html(style)
    # 固定列宽 + 深色文字，避免字体缺失导致的换行差异影响结论
    html = html.replace("<body>", '<body style="font-family: sans-serif; margin: 0">')

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 700, "height": 900})
        page.set_content(html)
        page.wait_for_timeout(200)
        if args.screenshot:
            page.screenshot(path=args.screenshot, full_page=True)
        result = page.evaluate(MEASURE_JS)
        browser.close()

    rows = result["alerts"]
    containers = result["containers"]

    if not rows:
        print("[FAIL] 夹具里没有任何 .el-alert，说明样式/标记已失效，脚本需要更新")
        return 1

    failures: list[str] = []
    print(f"{'夹具':<8}{'实际高':>7}{'需要高':>7}{'shrink':>8}  文案")
    for row in rows:
        # 两个判据缺一不可：scrollHeight 比较抓"内容装不下"，物理下限抓"整块被压到刚好卡住"
        # （后者 scrollHeight 会跟着一起缩，两者相等，只看第一个判据会漏）。
        clipped = row["height"] < row["scrollHeight"] - 1 or row["height"] < MIN_ALERT_HEIGHT
        mark = "FAIL" if clipped else "ok"
        print(
            f"{mark:<5}{row['fixture']:<8}{row['height']:>7}{row['scrollHeight']:>9}{row['shrink']:>8}  {row['text']}"
        )
        if clipped:
            failures.append(
                f"提示条被压缩：高 {row['height']}px，需要 {row['scrollHeight']}px"
                f"（下限 {MIN_ALERT_HEIGHT}px），fixture={row['fixture']}"
            )

    if failures:
        print("\n[FAIL] " + f"共 {len(failures)} 条提示条不满足「完整显示」契约：")
        for item in failures:
            print("  - " + item)
        print("  修法：`.lc-scroll > *, .lc-alerts > * { flex-shrink: 0 }`（见组件样式注释）")
        return 1

    # 防空跑：没压缩、但夹具本身也没溢出 → 说明夹具与组件结构已脱节，pass 毫无意义
    vacuous = [
        f"{c['fixture']}: clientHeight={c['clientHeight']} 未被内容撑破"
        for c in containers
        if c["scrollHeight"] <= c["clientHeight"]
    ]
    if vacuous:
        print("[FAIL] 夹具未制造溢出，本次结论无效（结构已变，脚本需要更新）：")
        for item in vacuous:
            print("  - " + item)
        return 1

    print(f"\n[PASS] {len(rows)} 条提示条均完整显示（clientHeight ≥ scrollHeight 且 ≥ {MIN_ALERT_HEIGHT}px）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
