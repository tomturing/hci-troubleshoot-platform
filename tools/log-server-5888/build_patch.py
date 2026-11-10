"""从基线 app.py 与权威 snippet 生成 app.py.patch，保证两者逐字一致。

用法（在平台仓库本机执行，需先 scp 现网 app.py 到临时路径）：

    uv run --no-project python tools/log-server-5888/build_patch.py \
        --baseline /tmp/acli_app_baseline.py \
        --output tools/log-server-5888/app.py.patch

为什么不让人工手写 diff：补丁要打到生产单进程上传服务上，行号/上下文
错一行就可能让 5888 起不来；由 difflib 从真实基线生成，再用
``patch --dry-run`` 与 ``py_compile`` 双重校验，才能把「手滑」排除掉。
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
from pathlib import Path

SNIPPET_MARKER = "# ---- 以下为插入 app.py 的源码"
OLD_IMPORT = "from datetime import datetime, timedelta\n"
NEW_IMPORT = "from datetime import datetime, timedelta, timezone\n"
INSERT_BEFORE = "@app.route('/healthz', methods=['GET'])\n"


def extract_block(snippet_text: str) -> str:
    """截取插入代码块，去掉本文件里给人看的模块 docstring。"""
    lines = snippet_text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith(SNIPPET_MARKER):
            block = "".join(lines[index + 1 :]).lstrip("\n")
            return block if block.endswith("\n") else block + "\n"
    raise SystemExit("错误：snippet 缺少插入标记，拒绝生成补丁")


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 5888 进度端点补丁")
    parser.add_argument("--baseline", required=True, help="现网 app.py 的只读副本")
    parser.add_argument("--snippet", default=str(Path(__file__).with_name("upload_progress.snippet.py")))
    parser.add_argument("--output", required=True, help="生成的 unified diff 路径")
    args = parser.parse_args()

    baseline = Path(args.baseline).read_text(encoding="utf-8")
    lines = baseline.splitlines(keepends=True)

    if "def upload_progress():" in baseline:
        raise SystemExit("错误：基线已含 upload_progress，拒绝生成重复补丁")
    if lines.count(OLD_IMPORT) != 1:
        raise SystemExit("错误：datetime import 行不唯一或已改过，拒绝盲改")
    if lines.count(INSERT_BEFORE) != 1:
        raise SystemExit("错误：/healthz 锚点不唯一，拒绝盲插")

    block = extract_block(Path(args.snippet).read_text(encoding="utf-8"))
    updated: list[str] = []
    for line in lines:
        if line == OLD_IMPORT:
            updated.append(NEW_IMPORT)
        elif line == INSERT_BEFORE:
            # 必须逐行展开：整块当一个元素时 difflib 会把 164 行算成 1 行，
            # 生成的 hunk 头行数错误，patch --dry-run 会直接 FAILED。
            updated.extend(block.splitlines(keepends=True))
            updated.append("\n")
            updated.append(line)
        else:
            updated.append(line)

    diff = list(
        difflib.unified_diff(
            lines,
            updated,
            fromfile="a/app.py",
            tofile="b/app.py",
            n=3,
        )
    )
    Path(args.output).write_text("".join(diff), encoding="utf-8")

    print(f"基线 sha256={hashlib.sha256(baseline.encode()).hexdigest()}")
    print(f"补丁行数={len(diff)} 插入代码块行数={len(block.splitlines())}")
    print(f"已写入 {args.output}")


if __name__ == "__main__":
    main()
