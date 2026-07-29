"""
tests/unit/kbd/test_pipeline_semantic.py — pipeline 语义化核心 + ImportError 回归防护

覆盖（对应 PR538 审查修复项）：
  - #1  防护：import kbd.pipeline 不得抛 ImportError（曾因
        image_proc.get_failed_vision_ids 被删除仍被导入而崩溃）
  - #12 语义统一核心 _html_to_semantic_text：段落/列表/表格/图片占位符/样式剥离
  - #12 convert_kbd_structured：返回结构化字段 + images_json(seq/section/desc) +
        content_md 恒为 None（交由后端 rebuild_content_md 渲染）
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

# 将 data-pipeline/ 加入路径，使 `from kbd.xxx import ...` 可用
_data_pipeline_root = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "data-pipeline")
)
if _data_pipeline_root not in sys.path:
    sys.path.insert(0, _data_pipeline_root)

from tests.unit.kbd.conftest import MINIMAL_9_SECTION_HTML  # noqa: E402

_IMG_ABS = "https://support.sangfor.com.cn/_static/202601/img1.png"
_FAKE_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"


# ─── #1 防护：pipeline 可被 import（无 ImportError） ─────────────────────────

class TestPipelineImportable:
    def test_import_pipeline_no_import_error(self):
        """import kbd.pipeline 不应抛 ImportError（PR538 #1 回归防护）。"""
        import importlib

        mod = importlib.import_module("kbd.pipeline")
        assert hasattr(mod, "run_pipeline")


class TestSignalDocumentStatus:
    def test_requires_nonempty_signals_and_verification_contract(self):
        from kbd.pipeline import _signal_document_status

        assert _signal_document_status(None) == "failed"
        assert _signal_document_status("not-json") == "failed"
        assert _signal_document_status({"schema_version": 2, "signals": []}) == "needs_review"
        assert _signal_document_status({"schema_version": 2, "signals": [{}]}) == "needs_review"
        assert _signal_document_status({
            "schema_version": 2,
            "signals": [{"id": "s1"}],
            "verification_contract": {"schema_version": 1},
        }) == "done"


# ─── #12 语义统一核心 _html_to_semantic_text ───────────────────────────────

class TestHtmlToSemanticText:
    def setup_method(self):
        from kbd.converter import _html_to_semantic_text

        self.fn = _html_to_semantic_text

    def test_paragraph_and_style_stripped(self):
        html = (
            '<div><p class="x" style="color:red">网口频繁<b>闪断</b>告警</p>'
            "<script>var a=1;</script>"
            "<style>.a{color:red}</style></div>"
        )
        out = self.fn(html, {})
        # 行内文本以空格分隔（正常语义化行为）
        assert "网口频繁" in out and "闪断" in out and "告警" in out
        # 装饰样式 / script / style 被丢弃
        assert "var a=1" not in out
        assert "color:red" not in out

    def test_list_rendered_with_dash(self):
        html = "<ul><li>登录主机</li><li>查看网口状态</li></ul>"
        out = self.fn(html, {})
        assert "- 登录主机" in out
        assert "- 查看网口状态" in out

    def test_table_rendered_as_kv(self):
        html = (
            "<table><tr><td>字段A</td><td>值1</td></tr>"
            "<tr><td>字段B</td><td>值2</td></tr></table>"
        )
        out = self.fn(html, {})
        assert "- 字段A: 值1" in out
        assert "- 字段B: 值2" in out

    def test_image_replaced_with_placeholder(self):
        html = f'<div><p>见下图</p><img src="{_IMG_ABS}" /></div>'
        image_map = {_IMG_ABS: {"seq": 0, "desc": ""}}
        out = self.fn(html, image_map)
        assert "![img:0]" in out

    def test_empty_html_returns_empty(self):
        assert self.fn("", {}) == ""
        assert self.fn("   ", {}) == ""


# ─── #12 convert_kbd_structured：结构化 + content_md 恒 None ─────────────────

class TestConvertKbdStructured:
    def _write_raw(self, tmp_path, content_html):
        case_dir = tmp_path / "36156"
        case_dir.mkdir(parents=True, exist_ok=True)
        rows = {
            "id": 36156,
            "name": "测试案例",
            "content": content_html,
            "mainModuleNames": "网络问题",
        }
        (case_dir / "raw.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )
        return "36156"

    def test_returns_structured_with_content_md_none_and_images(self, tmp_path):
        from kbd import converter

        support_id = self._write_raw(tmp_path, MINIMAL_9_SECTION_HTML)

        fake_map = {_IMG_ABS: {"seq": 0, "desc": ""}}

        with patch.object(converter, "_build_image_seq_map", return_value=fake_map), patch.object(
            converter, "_load_image_base64", return_value={"mime_type": "image/png", "data_base64": _FAKE_B64}
        ):
            converter.settings.KBD_CACHE_DIR = tmp_path
            result = converter.convert_kbd_structured(support_id)

        assert result is not None, "必填齐全的 case 不应返回 None"
        # content_md 必须由后端重建，pipeline 不生成（PR538 语义统一核心约定）
        assert result["content_md"] is None
        # 8 大章节字段存在
        for field in (
            "problem_description",
            "alert_info",
            "steps_text",
            "root_cause",
            "solution",
            "operational_impact",
            "is_temporary",
            "recommendations",
        ):
            assert field in result
        # images_json：章节、上下文与 desc；desc 初始为空（VISION 阶段填充）
        assert len(result["images_json"]) == 1
        img0 = result["images_json"][0]
        assert img0["seq"] == 0
        assert img0["section"] == "alert_info"  # 该图位于「告警信息」章节
        assert "context_before" in img0
        assert "context_after" in img0
        assert img0["desc"] == ""
        # 图片二进制随结构化数据返回（IMPORT 原子写入 kbd_image）
        assert len(result["images"]) == 1
        assert result["images"][0]["data_base64"] == _FAKE_B64
        # 章节字段中保留了图片占位符
        assert "![img:0]" in result["alert_info"]

    def test_missing_mandatory_section_returns_none(self, tmp_path):
        from kbd import converter

        # 缺少「有效排查步骤」必填章节
        broken_html = """
        <div class="mceNonEditable"><input readonly type="text" value="*问题描述" />
          <div contenteditable="true">有问题描述</div></div>
        <div class="mceNonEditable"><input readonly type="text" value="有效排查步骤" />
          <div contenteditable="true"> </div></div>
        <div class="mceNonEditable"><input readonly type="text" value="*解决方案" />
          <div contenteditable="true">有解决方案</div></div>
        """
        support_id = self._write_raw(tmp_path, broken_html)
        with patch.object(converter, "_build_image_seq_map", return_value={}), patch.object(
            converter, "_load_image_base64", return_value=None
        ):
            converter.settings.KBD_CACHE_DIR = tmp_path
            assert converter.convert_kbd_structured(support_id) is None
