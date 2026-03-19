"""
reviewer_cli.py — 交互式人工抽查工具

功能：
  - 从 enriched/ 随机抽样 N 篇展示内容摘要
  - 支持 y（通过）/ n（拒绝）/ s（跳过）/ q（退出）交互操作
  - 通过的记录追加到 reviewed.jsonl
  - 拒绝的记录追加到 rejected.jsonl（含拒绝原因）
  - 已审核的 ID 跳过重复展示

使用：
  uv run data-pipeline/reviewer_cli.py --sample 20   # 随机抽 20 篇
  uv run data-pipeline/reviewer_cli.py --all          # 全量审核
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import textwrap
from pathlib import Path

ENRICHED_DIR = Path(__file__).parent / "enriched"
REVIEWED_FILE = Path(__file__).parent / "reviewed.jsonl"
REJECTED_FILE = Path(__file__).parent / "rejected.jsonl"

# ANSI 颜色（终端支持时有效）
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _load_reviewed_ids() -> set[str]:
    """加载已审核 ID（reviewed + rejected）"""
    ids: set[str] = set()
    for path in (REVIEWED_FILE, REJECTED_FILE):
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for line in f:
                    with contextlib.suppress(json.JSONDecodeError, KeyError):
                        ids.add(json.loads(line.strip())["id"])
    return ids


def _show_doc(doc: dict, idx: int, total: int) -> None:
    """在终端展示案例摘要"""
    print(f"\n{'='*60}")
    print(f"{CYAN}[{idx}/{total}] ID: {doc['id']}{RESET}")
    print(f"标题     : {doc.get('title', '(无)')[:80]}")
    print(f"故障类别 : {doc.get('fault_category', '?')}")
    print(f"关键词   : {', '.join(doc.get('keywords', []))[:60]}")
    print(f"来源     : {doc.get('source_url', '(无)')}")
    if doc.get("summary"):
        print(f"摘要     : {doc['summary'][:120]}")
    # 正文前 300 字
    content_preview = doc.get("content_md", "")[:300]
    print(f"\n{YELLOW}内容预览（前300字）：{RESET}")
    print(textwrap.fill(content_preview, width=70, subsequent_indent="  "))
    print()


def run(sample: int | None = 20, review_all: bool = False) -> None:
    """
    运行交互式审核。

    Args:
        sample: 随机抽样数量（None 表示不限）
        review_all: True 则遍历全部未审核文件
    """
    json_files = sorted(ENRICHED_DIR.glob("*.json"))
    if not json_files:
        print(f"{RED}enriched/ 目录为空，请先运行 enricher.py{RESET}")
        return

    reviewed_ids = _load_reviewed_ids()

    # 过滤未审核
    pending = [f for f in json_files if f.stem not in reviewed_ids]
    print(f"待审核 {len(pending)} 篇（已跳过 {len(reviewed_ids)} 篇已审核）")

    if not pending:
        print(f"{GREEN}全部案例均已审核！{RESET}")
        return

    # 抽样
    if not review_all and sample is not None and len(pending) > sample:
        pending = random.sample(pending, sample)
        print(f"随机抽取 {sample} 篇进行审核")

    total = len(pending)
    reviewed_count = 0
    rejected_count = 0

    for idx, path in enumerate(pending, 1):
        doc = json.loads(path.read_text(encoding="utf-8"))
        _show_doc(doc, idx, total)

        while True:
            try:
                choice = input("审核结果 [y通过 / n拒绝 / s跳过 / q退出]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\n已中止")
                return

            if choice == "y":
                with REVIEWED_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(doc, ensure_ascii=False) + "\n")
                print(f"{GREEN}✓ 已通过{RESET}")
                reviewed_count += 1
                break
            elif choice == "n":
                reason = input("拒绝原因（可选）: ").strip()
                record = {**doc, "_rejected_reason": reason}
                with REJECTED_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(f"{RED}✗ 已拒绝{RESET}")
                rejected_count += 1
                break
            elif choice == "s":
                print(f"{YELLOW}→ 已跳过{RESET}")
                break
            elif choice == "q":
                print("退出审核")
                _print_summary(reviewed_count, rejected_count, total)
                return
            else:
                print("无效输入，请输入 y/n/s/q")

    _print_summary(reviewed_count, rejected_count, total)


def _print_summary(reviewed: int, rejected: int, total: int) -> None:
    print(f"\n{'─'*40}")
    print(f"审核汇总：通过 {GREEN}{reviewed}{RESET}，拒绝 {RED}{rejected}{RESET}，"
          f"共处理 {reviewed + rejected}/{total} 篇")
    print(f"通过结果已追加至: {REVIEWED_FILE}")
    if rejected:
        print(f"拒绝结果已追加至: {REJECTED_FILE}")


def main() -> None:
    parser = argparse.ArgumentParser(description="交互式人工抽查审核工具")
    parser.add_argument("--sample", type=int, default=20, help="随机抽样数量（默认 20）")
    parser.add_argument("--all", dest="review_all", action="store_true", help="审核全部未审核文件")
    args = parser.parse_args()

    run(sample=args.sample, review_all=args.review_all)


if __name__ == "__main__":
    main()
