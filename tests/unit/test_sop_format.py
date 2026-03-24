"""
tests/unit/test_sop_format.py — SOP 格式修复脚本单元测试

测试目标：scripts/dev/fix_sop_format.py

完成标准：
  - pytest tests/unit/test_sop_format.py -v 全部绿色 PASSED
  - 双重文本检测函数正确识别各种变体
  - 无误报（正常行不被误删）
  - 文件级修复端到端验证
"""

from __future__ import annotations

# 确保能导入 scripts/dev 模块
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts" / "dev"))

from fix_sop_format import (
    _fix_doubled_bullet,
    _last_bullet_text,
    _remove_standalone_duplicates,
    fix_file,
)

# --------------------------------------------------------------------------- #
# _fix_doubled_bullet 测试
# --------------------------------------------------------------------------- #

class TestFixDoubledBullet:
    """测试列表项双重文本修复。"""

    def test_simple_doubled_command(self):
        """最典型：- cmd cmd → - cmd"""
        line = "- acli vm disk path get -v ${vmid}acli vm disk path get -v ${vmid}"
        fixed, changed = _fix_doubled_bullet(line)
        assert changed is True
        assert fixed == "- acli vm disk path get -v ${vmid}"

    def test_simple_doubled_with_indent(self):
        """带缩进的列表项也能修复。"""
        line = "  - acli system df | grep ${storageid}acli system df | grep ${storageid}"
        fixed, changed = _fix_doubled_bullet(line)
        assert changed is True
        assert fixed == "  - acli system df | grep ${storageid}"

    def test_normal_command_not_modified(self):
        """普通（不重复）命令不被误修改。"""
        line = "- acli vm list | grep vmid"
        fixed, changed = _fix_doubled_bullet(line)
        assert changed is False
        assert fixed == line

    def test_chinese_bullet_not_modified(self):
        """中文说明文字不被误修改。"""
        line = "- 检查控制台告警：存储掉线告警"
        fixed, changed = _fix_doubled_bullet(line)
        assert changed is False
        assert fixed == line

    def test_heading_not_modified(self):
        """标题行不被处理。"""
        line = "## 存储离线"
        fixed, changed = _fix_doubled_bullet(line)
        assert changed is False
        assert fixed == line

    def test_empty_line_not_modified(self):
        """空行不被处理。"""
        line = ""
        fixed, changed = _fix_doubled_bullet(line)
        assert changed is False
        assert fixed == ""

    def test_odd_length_not_modified(self):
        """奇数长度文本不会误判为双重。"""
        line = "- abc"
        fixed, changed = _fix_doubled_bullet(line)
        assert changed is False

    def test_similar_but_not_doubled(self):
        """相似但非完全重复的文本不被修改。"""
        line = "- acli vm list | grep vmidacli vm list | grep vmid2"
        fixed, changed = _fix_doubled_bullet(line)
        assert changed is False


# --------------------------------------------------------------------------- #
# _last_bullet_text 测试
# --------------------------------------------------------------------------- #

class TestLastBulletText:
    def test_finds_last_bullet(self):
        lines = ["### 标题\n", "- 查看存储状态\n", "  \n"]
        assert _last_bullet_text(lines) == "查看存储状态"

    def test_stops_at_heading(self):
        lines = ["- 旧命令\n", "### 新章节\n", "  \n"]
        assert _last_bullet_text(lines) == ""

    def test_empty_list(self):
        assert _last_bullet_text([]) == ""


# --------------------------------------------------------------------------- #
# _remove_standalone_duplicates 测试
# --------------------------------------------------------------------------- #

class TestRemoveStandaloneDuplicates:
    def test_removes_duplicate_bare_line_before_code_block(self):
        """裸命令行与上方 bullet 重复且后跟代码块时，应被删除。"""
        lines = [
            "- acli vm list\n",
            "acli vm list\n",        # ← 应被删除
            "```bash\n",
            "acli vm list\n",
            "```\n",
        ]
        result, removed = _remove_standalone_duplicates(lines)
        assert removed == 1
        assert result == [
            "- acli vm list\n",
            "```bash\n",
            "acli vm list\n",
            "```\n",
        ]

    def test_keeps_bare_line_not_matching_bullet(self):
        """裸命令行内容与上方 bullet 不同时，保留。"""
        lines = [
            "- 查看虚拟机列表\n",
            "acli vm status get\n",   # 内容不同于 bullet
            "```bash\n",
            "acli vm status get\n",
            "```\n",
        ]
        result, removed = _remove_standalone_duplicates(lines)
        assert removed == 0
        assert len(result) == 5

    def test_no_false_positive_after_heading(self):
        """标题后的普通文本不被误删。"""
        lines = [
            "#### 解决方案\n",
            "联系技术支持团队。\n",  # 不冗余
        ]
        result, removed = _remove_standalone_duplicates(lines)
        assert removed == 0
        assert result == lines


# --------------------------------------------------------------------------- #
# fix_file 端到端测试
# --------------------------------------------------------------------------- #

class TestFixFile:
    def test_fixes_real_doubled_content(self, tmp_path: Path):
        """端到端：写入含问题的 md 文件，执行修复，验证结果。"""
        content = textwrap.dedent("""\
            ### 存储ID不可访问

            #### 判断方法
            1、查看虚拟机存储镜像目录，获取镜像目录挂载点
            - acli vm disk path get -v ${vmid}acli vm disk path get -v ${vmid}
            ```bash
            acli vm disk path get -v ${vmid}
            ```
        """)
        md_file = tmp_path / "test_chapter.md"
        md_file.write_text(content, encoding="utf-8")

        fixes = fix_file(md_file, dry_run=False)
        assert fixes > 0

        result = md_file.read_text(encoding="utf-8")
        # 双重命令已修复
        assert "acli vm disk path get -v ${vmid}acli vm disk path get -v ${vmid}" not in result
        # 命令本体保留
        assert "acli vm disk path get -v ${vmid}" in result

    def test_clean_file_unchanged(self, tmp_path: Path):
        """无问题的文件 fix_file 返回 0，内容不变。"""
        content = textwrap.dedent("""\
            ### 正常章节

            #### 判断方法
            - 检查存储状态
            ```bash
            acli storage status get
            ```
        """)
        md_file = tmp_path / "clean.md"
        original = content
        md_file.write_text(original, encoding="utf-8")

        fixes = fix_file(md_file, dry_run=False)
        assert fixes == 0
        assert md_file.read_text(encoding="utf-8") == original

    def test_dry_run_does_not_write(self, tmp_path: Path):
        """dry_run=True 时，发现问题但不写入文件。"""
        content = "- cmdcmd\n"
        md_file = tmp_path / "dry.md"
        md_file.write_text(content, encoding="utf-8")

        fixes = fix_file(md_file, dry_run=True)
        assert fixes > 0
        # 内容不变（未写入）
        assert md_file.read_text(encoding="utf-8") == content
