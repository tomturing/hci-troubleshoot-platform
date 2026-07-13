"""
tests/unit/kbd/test_converter.py — kbd/converter.py 单元测试

覆盖：
  - _parse_sections：从 HTML 解析 9 个 section（真实 DOM 结构）
  - _is_empty_content：空内容检测（空格/空标签/None）
  - _build_image_seq_map：按全局顺序建立 img URL → vision_desc 映射
  - _html_to_md：HTML 转 Markdown（img→vision块、基础格式）
  - convert_kbd：主流程（文件读取 + 必填验证 + content_md 组装）
  - convert_kbd_with_meta：返回完整元数据字典
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

_scripts_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))
if _scripts_root not in sys.path:
    sys.path.insert(0, _scripts_root)

# 从 conftest 中引用样本 HTML
from tests.unit.kbd.conftest import (
    MINIMAL_9_SECTION_HTML,
)

# ─── _parse_sections ─────────────────────────────────────────────────────────


class TestParseSections:
    """测试 HTML → section dict 解析"""

    def setup_method(self):
        from kbd.converter import _parse_sections

        self.fn = _parse_sections

    def test_parses_all_9_sections(self):
        """完整 HTML 中能解析出全部 9 个 section"""
        result = self.fn(MINIMAL_9_SECTION_HTML)
        expected_titles = [
            "问题描述",
            "告警信息",
            "有效排查步骤",
            "根因",
            "解决方案",
            "操作影响范围",
            "是否是临时解决方案",
            "建议与总结",
            "排查内容",
        ]
        for title in expected_titles:
            assert title in result, f"缺少 section: {title}"

    def test_mandatory_sections_have_content(self):
        """必填 section 的内容不为空"""
        result = self.fn(MINIMAL_9_SECTION_HTML)
        for mandatory in ["问题描述", "有效排查步骤", "解决方案"]:
            from bs4 import BeautifulSoup

            text = BeautifulSoup(result[mandatory], "lxml").get_text(strip=True)
            assert text, f"必填 section '{mandatory}' 内容为空"

    def test_empty_html_returns_empty_dict(self):
        assert self.fn("") == {}
        assert self.fn("<p>无section</p>") == {}

    def test_unknown_section_ignored(self):
        """未知 section 不出现在结果中"""
        html = """
        <div class="mceNonEditable">
          <input type="text" value="自定义未知段落" />
          <div>内容</div>
        </div>
        """
        result = self.fn(html)
        assert "自定义未知段落" not in result


# ─── _is_empty_content ───────────────────────────────────────────────────────


class TestIsEmptyContent:
    """测试空内容识别"""

    def setup_method(self):
        from kbd.converter import _is_empty_content

        self.fn = _is_empty_content

    def test_none_is_empty(self):
        assert self.fn(None) is True

    def test_empty_string_is_empty(self):
        assert self.fn("") is True

    def test_whitespace_only_is_empty(self):
        assert self.fn("   ") is True
        assert self.fn("\n  \n") is True

    def test_whitespace_div_is_empty(self):
        assert self.fn("<div> </div>") is True
        assert self.fn('<div contenteditable="true"> </div>') is True

    def test_text_content_not_empty(self):
        assert self.fn("<div>有内容</div>") is False
        assert self.fn("普通文本") is False

    def test_img_only_is_not_empty(self):
        """只有图片的 section 不视为空（图片本身就是内容）"""
        html = '<div><img src="/img.png" /></div>'
        assert self.fn(html) is False


# ─── _build_image_seq_map ────────────────────────────────────────────────────


class TestBuildImageSeqMap:
    """测试全局图片序号到 vision_desc 的映射"""

    def test_with_desc_files(self, tmp_path):
        html = """
        <img src="/_static/img1.png" />
        <img src="/_static/img2.png" />
        """
        (tmp_path / "img_0.desc.txt").write_text("第一张图说明", encoding="utf-8")
        (tmp_path / "img_1.desc.txt").write_text("第二张图说明", encoding="utf-8")

        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path.parent),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import _build_image_seq_map

            img_map = _build_image_seq_map(tmp_path.name, html)

        assert len(img_map) == 2
        values = list(img_map.values())
        # 返回格式已变更：{url: {"seq": int, "desc": str}}
        assert values[0]["desc"] == "第一张图说明"
        assert values[1]["desc"] == "第二张图说明"

    def test_missing_desc_file_returns_empty_string(self, tmp_path):
        """没有 desc 文件时对应 URL 的 value 为空字符串"""
        html = '<img src="/_static/img1.png" />'
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path.parent),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import _build_image_seq_map

            img_map = _build_image_seq_map(tmp_path.name, html)
        # 返回格式已变更：{url: {"seq": int, "desc": str}}
        values = list(img_map.values())
        assert values == [{"seq": 0, "desc": ""}]

    def test_no_images(self, tmp_path):
        html = "<p>没有图片</p>"
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path.parent),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import _build_image_seq_map

            assert _build_image_seq_map(tmp_path.name, html) == {}


# ─── _html_to_md ─────────────────────────────────────────────────────────────


class TestHtmlToMd:
    """测试 HTML → Markdown 转换"""

    def setup_method(self):
        from kbd.converter import _html_to_md

        self.fn = _html_to_md

    def test_basic_text(self):
        md = self.fn("<p>普通段落</p>", {})
        assert "普通段落" in md

    def test_empty_html_returns_empty(self):
        assert self.fn("", {}) == ""
        assert self.fn("   ", {}) == ""

    def test_list_converted(self):
        html = "<ul><li>第一项</li><li>第二项</li></ul>"
        md = self.fn(html, {})
        assert "第一项" in md
        assert "第二项" in md

    def test_img_with_desc_replaced(self):
        """有 vision_desc 的 img 应替换为引用块"""
        html = '<img src="/_static/img1.png" />'
        # image_map 格式已变更：{url: {"seq": int, "desc": str}}
        image_map = {"https://support.sangfor.com.cn/_static/img1.png": {"seq": 0, "desc": "服务器面板截图"}}
        with patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"):
            md = self.fn(html, image_map)
        assert "截图说明" in md
        assert "服务器面板截图" in md

    def test_img_without_desc_shown_as_placeholder(self):
        """无 desc 的 img 应显示为 [图片] 占位"""
        html = '<img src="/_static/img999.png" />'
        with patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"):
            md = self.fn(html, {})
        assert "图片" in md

    def test_multiple_newlines_collapsed(self):
        """连续多个换行应被规范化为最多两个"""
        html = "<p>第一段</p><p></p><p></p><p>第二段</p>"
        md = self.fn(html, {})
        assert "\n\n\n" not in md


# ─── convert_kbd ────────────────────────────────────────────────────────────


class TestConvertKbd:
    """测试 convert_kbd 主流程"""

    def test_success_returns_content_md(self, tmp_path, minimal_rows):
        """正常案例应返回非空 content_md 字符串"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(json.dumps(minimal_rows), encoding="utf-8")
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import convert_kbd

            result = convert_kbd("36156")

        assert isinstance(result, str)
        assert len(result) > 0
        assert "问题描述" in result
        assert "有效排查步骤" in result
        assert "解决方案" in result

    def test_missing_raw_json_returns_none(self, tmp_path):
        """raw.json 不存在时应返回 None"""
        with patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path):
            from kbd.converter import convert_kbd

            result = convert_kbd("nonexistent")
        assert result is None

    def test_missing_mandatory_section_returns_none(self, tmp_path, missing_mandatory_rows):
        """必填 section 缺失时应返回 None 并写 abnormal.json"""
        case_dir = tmp_path / "missing"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(json.dumps(missing_mandatory_rows), encoding="utf-8")
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import convert_kbd

            result = convert_kbd("missing")

        assert result is None
        abnormal_path = tmp_path / "missing" / "abnormal.json"
        assert abnormal_path.exists()
        record = json.loads(abnormal_path.read_text(encoding="utf-8"))
        assert "有效排查步骤" in record["missing_sections"]

    def test_empty_optional_sections_excluded(self, tmp_path, minimal_rows):
        """内容为空白的可选 section（如建议与总结）不出现在 content_md 中"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(json.dumps(minimal_rows), encoding="utf-8")
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import convert_kbd

            result = convert_kbd("36156")

        # 建议与总结和排查内容是空白的，不应出现
        assert "建议与总结" not in result
        assert "排查内容" not in result

    def test_vision_desc_embedded(self, tmp_path, minimal_rows):
        """有 vision desc 文件时，content_md 应包含视觉描述"""
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(json.dumps(minimal_rows), encoding="utf-8")
        # 为第 0 张图写描述（告警信息 section 的第一张图）
        (case_dir / "img_0.desc.txt").write_text("告警页面截图，显示红色网口闪断告警", encoding="utf-8")
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import convert_kbd

            result = convert_kbd("36156")

        assert "截图说明" in result
        assert "告警页面截图" in result


# ─── convert_kbd_with_meta ──────────────────────────────────────────────────


class TestConvertKbdWithMeta:
    """测试 convert_kbd_with_meta 返回结构"""

    def test_returns_expected_keys(self, tmp_path, minimal_rows):
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(json.dumps(minimal_rows), encoding="utf-8")
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import convert_kbd_with_meta

            result = convert_kbd_with_meta("36156")

        assert result is not None
        for key in ["support_id", "title", "content_md", "metadata"]:
            assert key in result, f"缺少 key: {key}"

    def test_metadata_populated(self, tmp_path, minimal_rows):
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(json.dumps(minimal_rows), encoding="utf-8")
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import convert_kbd_with_meta

            result = convert_kbd_with_meta("36156")

        meta = result["metadata"]
        assert meta["sangfor_main_module"] == "网络问题"
        assert meta["create_admin_id"] == "68532"

    def test_returns_none_when_mandatory_missing(self, tmp_path, missing_mandatory_rows):
        case_dir = tmp_path / "missing"
        case_dir.mkdir(parents=True)
        (case_dir / "raw.json").write_text(json.dumps(missing_mandatory_rows), encoding="utf-8")
        with (
            patch("kbd.converter.settings.KBD_CACHE_DIR", tmp_path),
            patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"),
        ):
            from kbd.converter import convert_kbd_with_meta

            result = convert_kbd_with_meta("missing")

        assert result is None


# ─── _normalize_screenshot_blocks ───────────────────────────────────────────


class TestNormalizeScreenshotBlocks:
    """
    测试 _normalize_screenshot_blocks：规范化截图块前导缩进。

    核心场景：markdownify 对嵌套列表内的 span[data-vision-desc] 生成带缩进的
    blockquote，本函数将其去掉，使截图块始终顶格输出。
    """

    def setup_method(self):
        from kbd.converter import _normalize_screenshot_blocks

        self.fn = _normalize_screenshot_blocks

    def test_no_indentation_unchanged(self):
        """顶格截图块不受影响"""
        md = "> **【截图说明】**\n> TYPE: 其他截图\n> BACKGROUND: 其他\n"
        assert self.fn(md) == md

    def test_indented_block_stripped(self):
        """带前导空格的截图块应全部去掉缩进"""
        md = (
            "      > **【截图说明】**\n"
            "      > TYPE: 其他截图\n"
            "      > BACKGROUND: 其他\n"
        )
        result = self.fn(md)
        for line in result.strip().splitlines():
            assert not line.startswith(" "), f"行仍带缩进: {repr(line)}"
        assert "> **【截图说明】**" in result
        assert "> TYPE: 其他截图" in result

    def test_block_ends_at_non_blockquote_line(self):
        """截图块后的普通文本不应被去掉缩进"""
        md = (
            "      > **【截图说明】**\n"
            "      > TYPE: 其他截图\n"
            "\n"
            "    - 普通列表项（保留缩进）\n"
        )
        result = self.fn(md)
        lines = result.splitlines()
        # 截图行顶格
        assert lines[0] == "> **【截图说明】**"
        assert lines[1] == "> TYPE: 其他截图"
        # 空行保留
        assert lines[2] == ""
        # 普通列表项保留原始缩进
        assert lines[3] == "    - 普通列表项（保留缩进）"

    def test_multiple_blocks_both_normalized(self):
        """文档中多个截图块都被规范化"""
        md = (
            "      > **【截图说明】**\n"
            "      > TYPE: 其他截图\n"
            "\n"
            "普通文本\n"
            "\n"
            "    > **【截图说明】**\n"
            "    > TYPE: 日志截图\n"
        )
        result = self.fn(md)
        lines = [line for line in result.splitlines() if line.strip()]
        screenshot_starts = [line for line in lines if "【截图说明】" in line]
        assert len(screenshot_starts) == 2
        for s in screenshot_starts:
            assert not s.startswith(" "), f"截图块起始行仍带缩进: {repr(s)}"

    def test_normal_blockquote_not_affected(self):
        """不含【截图说明】的普通 blockquote 不受影响"""
        md = "      > 普通引用内容\n      > 继续引用\n"
        result = self.fn(md)
        # 不以截图说明开头，不进入截图块模式，保留原样
        assert result == md

    def test_empty_string_returns_empty(self):
        assert self.fn("") == ""

    def test_plain_text_unchanged(self):
        """纯文本不受影响"""
        md = "这是一段普通文字\n\n- 列表项\n"
        assert self.fn(md) == md


# ─── _html_to_md 嵌套列表截图规范化（端到端）────────────────────────────────


class TestHtmlToMdNestedScreenshot:
    """
    端到端验证：嵌套列表内的截图块经 _html_to_md 转换后不带前导缩进。

    复现路径（KBD 27123 类型案例）：
      HTML: <ol><li><ul><li>判断依据：<img src="..."></li></ul></li></ol>
      markdownify 原始输出会对 img 替换后的 span 引用块加上多个前导空格。
      经 _normalize_screenshot_blocks 后，截图块必须顶格。
    """

    def setup_method(self):
        from kbd.converter import _html_to_md

        self.fn = _html_to_md

    def test_nested_list_screenshot_block_at_column_zero(self):
        """
        嵌套列表中的截图块（v2 格式）经转换后，
        '> **【截图说明】**' 必须出现在行首（无前导空格）。
        """
        html = """
        <ol>
          <li>
            <p>在 HCI 控制台查看虚拟机任务详情。</p>
            <ul>
              <li>判断依据：任务详情原样显示报错信息。
                <img src="/_static/img1.png" alt="截图">
              </li>
            </ul>
          </li>
        </ol>
        """
        desc_v2 = "TYPE: 其他截图\nBACKGROUND: 其他\nFULL_TEXT:\n- （无文字）\nDESCRIPTION:\n（无描述）"
        image_map = {
            "https://support.sangfor.com.cn/_static/img1.png": {"seq": 0, "desc": desc_v2}
        }
        with patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"):
            md = self.fn(html, image_map)

        # 核心断言：截图块起始行必须在列首，不带任何前导空格
        lines = md.splitlines()
        screenshot_starts = [line for line in lines if "【截图说明】" in line]
        assert len(screenshot_starts) >= 1, "应包含至少一个截图说明块"
        for line in screenshot_starts:
            assert line.startswith(">"), (
                f"截图块起始行带有前导缩进，前端解析器将无法识别: {repr(line)}"
            )

    def test_nested_list_all_screenshot_lines_at_column_zero(self):
        """截图块内所有后续 > 行也必须顶格"""
        html = """
        <ul>
          <li>步骤一
            <ul>
              <li>子步骤<img src="/_static/img2.png" alt="截图"></li>
            </ul>
          </li>
        </ul>
        """
        desc_v2 = "TYPE: 日志截图\nBACKGROUND: 黑色\nFULL_TEXT:\n- error: disk full\nDESCRIPTION:\n（无描述）"
        image_map = {
            "https://support.sangfor.com.cn/_static/img2.png": {"seq": 0, "desc": desc_v2}
        }
        with patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"):
            md = self.fn(html, image_map)

        in_screenshot = False
        for line in md.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("> **【截图说明】**"):
                in_screenshot = True
            if in_screenshot and stripped.startswith(">"):
                assert line == stripped, (
                    f"截图块内行带有前导空格: {repr(line)}"
                )
            elif in_screenshot and stripped and not stripped.startswith(">"):
                in_screenshot = False

