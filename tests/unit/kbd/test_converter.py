"""
tests/unit/kbd/test_converter.py — kbd/converter.py 单元测试

覆盖：
  - _parse_sections：从 HTML 解析 9 个 section（真实 DOM 结构）
  - _is_empty_content：空内容检测（空格/空标签/None）
  - _build_image_seq_map：按全局顺序建立 img URL → {"seq": int} 映射（不再读取 .desc.txt）

说明：
  legacy 旧路径（convert_kbd / convert_kbd_with_meta / _html_to_md /
  _normalize_screenshot_blocks / _load_vision_desc）已于 2026-07 彻底移除，
  其对应的测试（含 .desc.txt 文件读写）一并删除。图片视觉描述（desc）现由
  VISION 阶段（reanalyze）填充到 images_json，不再落本地文件。
"""

from __future__ import annotations

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


# ─── _build_image_seq_map ───────────────────────────────────────────────────


class TestBuildImageSeqMap:
    """测试全局图片序号映射（不再依赖本地 .desc.txt）"""

    def test_seq_mapping_without_desc(self, tmp_path):
        """图片应按全局顺序获得 seq，且不读取任何本地描述文件"""
        html = """
        <img src="/_static/img1.png" />
        <img src="/_static/img2.png" />
        """
        with patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"):
            from kbd.converter import _build_image_seq_map

            img_map = _build_image_seq_map(html)

        assert len(img_map) == 2
        values = list(img_map.values())
        # 仅含 seq，不含 desc（legacy .desc.txt 已移除）
        assert values[0] == {"seq": 0}
        assert values[1] == {"seq": 1}
        # 不应在 cache 目录留下任何 .desc.txt
        assert not list(tmp_path.glob("*.desc.txt"))

    def test_no_images(self):
        html = "<p>没有图片</p>"
        with patch("kbd.converter.settings.SANGFOR_API_BASE", "https://support.sangfor.com.cn"):
            from kbd.converter import _build_image_seq_map

            assert _build_image_seq_map(html) == {}
