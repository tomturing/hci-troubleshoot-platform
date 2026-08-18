"""判断 ArgoCD Application 是否仅发生已文档化的镜像晋级元数据更新。

promotion PR 只允许同时更新不可变 digest 和对应源码 revision。这类变更已在
docs/deploy/发布指南.md 文档化，不要求每个晋级 PR 重复同步 docs。

关键陷阱（历史 bug）：git diff 输出为 `-<缩进空格>digest: "sha256:..."`，减号后跟缩进空格。
若正则写成 `^[-+]?digest:` 会匹配失败（减号后不是 digest 而是空格），导致 digest 行无法被
过滤、豁免永远失效。本模块用 `^[-+][[:space:]]*digest:` 正确匹配带缩进的 digest 行。
"""

from __future__ import annotations

import re
import subprocess
import sys

# 仅豁免两个精确字段，其他 annotation 或镜像字段仍必须触发文档门禁。
_PROMOTION_LINE = re.compile(
    r'^[-+][\s]*(?:digest: "sha256:[0-9a-f]{64}"|hci-platform\.dev/image-source-revision: "(?:[0-9a-f]{40}|unverified-legacy)")$'
)
_DIGEST_LINE = re.compile(r'^[-+][\s]*digest: "sha256:[0-9a-f]{64}"$')
_SOURCE_LINE = re.compile(r'^[-+][\s]*hci-platform\.dev/image-source-revision: "(?:[0-9a-f]{40}|unverified-legacy)"$')


def _non_promotion_diff_lines(diff_body: str) -> list[str]:
    """返回 diff 中除受控晋级字段与文件头之外的所有 +/- 行。"""
    out: list[str] = []
    for line in diff_body.splitlines():
        if not line.startswith(("+", "-")):
            continue
        if line.startswith(("+++", "---")):
            continue
        if _PROMOTION_LINE.match(line):
            continue
        out.append(line)
    return out


def is_digest_pin_only(diff_body: str) -> bool:
    """只有 digest/source revision 成对原子更新时才允许豁免。"""
    changed = [
        line for line in diff_body.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    if _non_promotion_diff_lines(diff_body):
        return False
    digest_lines = [line for line in changed if _DIGEST_LINE.match(line)]
    source_lines = [line for line in changed if _SOURCE_LINE.match(line)]
    return (
        len(digest_lines) == 2
        and len(source_lines) == 2
        and {line[0] for line in digest_lines} == {"-", "+"}
        and {line[0] for line in source_lines} == {"-", "+"}
    )


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
        print(f"豁免（已文档化的镜像晋级元数据）：{file_path}")
        return 0
    print(f"非纯镜像晋级元数据更新，需同步文档：{file_path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
