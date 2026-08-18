"""判断单个 ArgoCD Application 清单文件是否仅发生 digest 锚点钉入（可豁免文档门禁）。

背景：自动钉 digest 动作（Renovate 式）会修改 deploy/gitops/argo-apps/** 下的
`digest: "sha256:..."` 行。这类改动已在 docs/deploy/发布指南.md 文档化，属于幂等发布动作，
不要求每个钉入 PR 内重复同步 docs。因此 docs-governance 需对「仅 digest 行变化」的文件豁免。

关键陷阱（历史 bug）：git diff 输出为 `-<缩进空格>digest: "sha256:..."`，减号后跟缩进空格。
若正则写成 `^[-+]?digest:` 会匹配失败（减号后不是 digest 而是空格），导致 digest 行无法被
过滤、豁免永远失效。本模块用 `^[-+][[:space:]]*digest:` 正确匹配带缩进的 digest 行。
"""

from __future__ import annotations

import re
import subprocess
import sys

# 匹配「-」或「+」开头、后跟任意空白、再跟 digest: "sha256:..." 的行（含缩进）。
_DIGEST_LINE = re.compile(r'^[-+][\s]*digest: "sha256:')


def _non_digest_diff_lines(diff_body: str) -> list[str]:
    """返回 diff 中除 digest 锚点行与文件头(+++|---)之外的所有 +/- 行。"""
    out: list[str] = []
    for line in diff_body.splitlines():
        if not line.startswith(("+", "-")):
            continue
        if line.startswith(("+++", "---")):
            continue
        if _DIGEST_LINE.match(line):
            continue
        out.append(line)
    return out


def is_digest_pin_only(diff_body: str) -> bool:
    """若文件的 diff 除 digest 锚点变化外无其他改动，返回 True（可豁免）。"""
    return len(_non_digest_diff_lines(diff_body)) == 0


def diff_for_file(base_sha: str, head_sha: str, file_path: str) -> str:
    """获取两 commit 间某文件的 git diff 文本。"""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", base_sha, head_sha, "--", file_path],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("用法: check_digest_pin_exempt.py <BASE_SHA> <HEAD_SHA> <FILE>", file=sys.stderr)
        return 2
    base_sha, head_sha, file_path = argv[1], argv[2], argv[3]
    diff_body = diff_for_file(base_sha, head_sha, file_path)
    if is_digest_pin_only(diff_body):
        print(f"豁免（已文档化的 digest 锚点钉入）：{file_path}")
        return 0
    print(f"非纯 digest 钉入，需同步文档：{file_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
