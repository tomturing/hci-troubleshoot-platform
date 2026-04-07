"""
fix_sop_format.py — 修复 SOP 章节 Markdown 文件的格式问题

问题描述：
  部分文件中，列表项里的 CLI 命令文本被重复拼接（原始内容渲染问题），例如：
      - acli vm disk path get -v ${vmid}acli vm disk path get -v ${vmid}
  修复后：
      - acli vm disk path get -v ${vmid}

  另外，同一 CLI 命令在列表项后会紧跟独立重复行（代码块前的说明已重复），
  例如在 ``` 代码块前出现多余的裸命令行，也会被清理。

用法：
    python scripts/dev/fix_sop_format.py [--dry-run] [目录路径]

    --dry-run   仅打印改动，不写入文件（默认值：不加则直接修复）
    目录路径    默认为 backend/kb-service/data/sop_skills/vm_start_failure/chapters/
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# 核心修复逻辑
# --------------------------------------------------------------------------- #

def _fix_doubled_bullet(line: str) -> tuple[str, bool]:
    """检测并修复列表项内文本翻倍问题。

    例如：
        输入：  "- acli cmd argacli cmd arg"
        输出：  ("- acli cmd arg", True)

    算法：
        取列表前缀（"- " 或 "  - " 等），对剩余内容尝试"前半 == 后半"匹配。
        若成立则返回修复后的行，否则原样返回。
    """
    # 只处理以 "- " 或 "  - " 开头的列表行
    m = re.match(r'^(\s*-\s+)(.+)$', line)
    if not m:
        return line, False

    prefix = m.group(1)
    text = m.group(2)

    # 尝试"完整文本 = 前半 + 后半"且 前半 == 后半
    n = len(text)
    if n >= 4 and n % 2 == 0:
        half = n // 2
        if text[:half] == text[half:]:
            return f"{prefix}{text[:half]}", True

    # 尝试各个分割点：若 text[split:] 恰好等于 text[:split]，则为精确翻倍
    for split in range(2, n):
        if text[split:] == text[:split]:
            return f"{prefix}{text[:split]}", True

    return line, False


def _remove_standalone_duplicates(lines: list[str]) -> tuple[list[str], int]:
    """清理代码块外、列表项后的重复裸命令行。

    场景：列表 bullet 后紧跟一或两行裸命令（与 bullet 文本相同），
    再跟 ```bash ... ``` 代码块，裸命令多余。

    规则：对于一行裸文本行（不以 `#`/`-`/`>`` 开头、不为空行、不在代码块内），
    若它与上方最近的列表 bullet 的文本相同（或是其子串），且其后紧跟
    代码块开始符 ```，则删除该裸文本行。
    """
    result: list[str] = []
    removed = 0
    in_code_block = False
    i = 0
    while i < len(lines):
        line = lines[i]
        # 追踪代码块状态（``` 切换开/关）
        stripped = line.strip()
        if stripped.startswith('```'):
            in_code_block = not in_code_block
            result.append(line)
            i += 1
            continue
        # 代码块内的行原样保留，不做任何处理
        if in_code_block:
            result.append(line)
            i += 1
            continue
        # 检测代码块外的裸文本行：不空、不以结构符开头
        is_bare = (
            stripped
            and not re.match(r'^\s*(#|-|\*|>)', line)
        )
        if is_bare and i + 1 < len(lines) and lines[i + 1].strip().startswith('```'):
            # 检查是否与上文 bullet 重复
            bullet_text = _last_bullet_text(result)
            bare_text = stripped
            if bullet_text and (bare_text == bullet_text or bullet_text.startswith(bare_text)):
                # 跳过此裸行
                removed += 1
                i += 1
                continue
        result.append(line)
        i += 1
    return result, removed


def _last_bullet_text(lines: list[str]) -> str:
    """从已处理行列表中向上查找最近一条列表项的文本内容。"""
    for line in reversed(lines):
        m = re.match(r'^\s*-\s+(.+)$', line)
        if m:
            return m.group(1).strip()
        if not line.strip():
            continue
        # 遇到标题行则停止查找
        if line.startswith('#'):
            break
    return ""


def fix_file(path: Path, dry_run: bool = False) -> int:
    """修复单个 Markdown 文件，返回修复行数。"""
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    fixed_lines: list[str] = []
    total_fixes = 0

    for line in lines:
        # 去掉行尾换行符来处理，处理后加回
        stripped = line.rstrip('\n')
        fixed, changed = _fix_doubled_bullet(stripped)
        if changed:
            total_fixes += 1
            print(f"  [双重文本] {path.name}:{lines.index(line) + 1}")
            print(f"    原始：{stripped!r}")
            print(f"    修复：{fixed!r}")
        newline_char = '\n' if line.endswith('\n') else ''
        fixed_lines.append(fixed + newline_char)

    fixed_lines_clean, removed = _remove_standalone_duplicates(fixed_lines)
    total_fixes += removed
    if removed:
        print(f"  [裸命令重复行] {path.name}: 清理 {removed} 行")

    if total_fixes == 0:
        return 0

    result = "".join(fixed_lines_clean)
    if not dry_run:
        path.write_text(result, encoding="utf-8")
    return total_fixes


def fix_directory(directory: Path, dry_run: bool = False) -> dict[str, int]:
    """递归修复目录下所有 .md 文件（跳过 .DEPRECATED 文件）。"""
    results: dict[str, int] = {}
    md_files = sorted(directory.rglob("*.md"))
    for file_path in md_files:
        if file_path.suffix == ".DEPRECATED":
            continue
        n = fix_file(file_path, dry_run=dry_run)
        if n > 0:
            results[str(file_path)] = n
    return results


# --------------------------------------------------------------------------- #
# CLI 入口
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="修复 SOP 章节 Markdown 格式问题")
    parser.add_argument(
        "directory",
        nargs="?",
        default="backend/kb-service/data/sop_skills/vm_start_failure/chapters",
        help="目标目录（默认：vm_power_failure/chapters）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印改动，不写入文件",
    )
    args = parser.parse_args()

    target = Path(args.directory)
    if not target.exists():
        print(f"错误：目录不存在：{target}", file=sys.stderr)
        sys.exit(1)

    mode = "（预览模式，不写入）" if args.dry_run else "（写入模式）"
    print(f"=== fix_sop_format {mode} ===")
    print(f"目标目录：{target.resolve()}\n")

    results = fix_directory(target, dry_run=args.dry_run)

    if results:
        print(f"\n共修复 {len(results)} 个文件，累计 {sum(results.values())} 处：")
        for f, n in results.items():
            print(f"  {f}: {n} 处")
    else:
        print("未发现需要修复的格式问题。")


if __name__ == "__main__":
    main()
