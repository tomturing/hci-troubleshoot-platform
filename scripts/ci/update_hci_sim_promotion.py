#!/usr/bin/env python3
"""同步更新 hci-sim GitOps digest 与对应源码 revision。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_LINE_RE = re.compile(r'^(?P<indent>\s*)digest: "sha256:[0-9a-f]{64}"$', re.MULTILINE)
SOURCE_LINE_RE = re.compile(
    r'^(?P<indent>\s*)hci-platform\.dev/image-source-revision: "(?:[0-9a-f]{40}|unverified-legacy)"$',
    re.MULTILINE,
)


def update_manifest(content: str, *, digest: str, source_sha: str) -> str:
    """只更新唯一的 digest/source revision 锚点，结构漂移时立即失败。"""
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError(f"非法镜像 digest：{digest}")
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise ValueError(f"非法源码 SHA：{source_sha}")
    if len(DIGEST_LINE_RE.findall(content)) != 1:
        raise ValueError("清单必须且只能包含一个 hci-sim digest 锚点")
    if len(SOURCE_LINE_RE.findall(content)) != 1:
        raise ValueError("清单必须且只能包含一个 image-source-revision 锚点")

    updated = DIGEST_LINE_RE.sub(lambda match: f'{match.group("indent")}digest: "{digest}"', content)
    return SOURCE_LINE_RE.sub(
        lambda match: f'{match.group("indent")}hci-platform.dev/image-source-revision: "{source_sha}"',
        updated,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()

    original = args.file.read_text(encoding="utf-8")
    updated = update_manifest(original, digest=args.digest, source_sha=args.source_sha)
    args.file.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
