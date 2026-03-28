#!/usr/bin/env python3
"""
CI 静态检查：确保 repo 层不直接调用 session.commit()

Unit of Work 规范（见 docs/guides/K3s集群健壮性改进计划.md F-1）：
  - Repository 层只允许 session.flush()，不允许 session.commit()
  - commit() 的唯一合法出口是 backend/shared/database/session.py 中的 get_session()

用法:
    cd hci-troubleshoot-platform
    python scripts/ci/check_session_commit.py         # 检查 backend/ 下所有 *_repo.py
    python scripts/ci/check_session_commit.py --fix   # 输出详细建议（不自动修改）

退出码:
    0 — 无违规
    1 — 发现违规，违规信息打印到 stdout
"""

import ast
import pathlib
import sys


def check_file(path: pathlib.Path) -> list[str]:
    """解析单个文件，返回违规行信息列表。"""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, OSError) as exc:
        return [f"{path}:? — 解析失败: {exc}"]

    violations = []
    for node in ast.walk(tree):
        # 匹配 `await <expr>.commit()` 形式
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        attr = getattr(func, "attr", "")
        if attr == "commit":
            violations.append(
                f"{path}:{node.lineno} — repo 层禁止调用 session.commit()，"
                f"请改用 session.flush()"
            )
    return violations


def main() -> int:
    backend_root = pathlib.Path(__file__).parent.parent.parent / "backend"
    if not backend_root.exists():
        print(f"错误：未找到 backend 目录（预期路径：{backend_root}）", file=sys.stderr)
        return 2

    all_violations: list[str] = []
    checked = 0

    for path in sorted(backend_root.rglob("*_repo.py")):
        checked += 1
        all_violations.extend(check_file(path))

    if all_violations:
        print(f"[check_session_commit] ❌ 发现 {len(all_violations)} 处违规（共检查 {checked} 个 repo 文件）:\n")
        for v in all_violations:
            print(f"  {v}")
        print(
            "\n说明：Repository 层只允许 session.flush()，"
            "commit() 必须由调用方（service 层 / get_session() 上下文管理器）统一管理。"
        )
        return 1

    print(f"[check_session_commit] ✅ 检查通过（共检查 {checked} 个 repo 文件，无违规）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
