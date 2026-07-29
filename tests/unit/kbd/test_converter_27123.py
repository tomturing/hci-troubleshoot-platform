"""
tests/unit/kbd/test_converter_27123.py — 27123 真实案例集成测试

27123（【HCI-VT】虚拟机开机失败）是触发 2026-07 KBD Pipeline P0-P2 重构的
根因案例：其嵌套列表内的截图 span 曾因 markdownify 缩进污染 content_md。

本文件基于 cache/27123 下的真实 raw.json + 图片，验证 `convert_kbd_structured`
（生产路径）在「.desc.txt 机制已彻底移除」后的真实行为：

  1. golden：真实数据完整转换，结构化字段 / images_json / images 正确。
  2. 回归：即便 cache 中存在 img_N.desc.txt，转换器也**忽略**它（desc 恒为空），
     证明 .desc.txt 本地文件机制已无残留依赖。
  3. 清理锁定：真实 cache/27123 目录不得再包含任何 .desc.txt 遗留文件。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

# 将 data-pipeline/ 目录加入路径，使 `from kbd.xxx import ...` 可用
_data_pipeline_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "data-pipeline"))
if _data_pipeline_root not in sys.path:
    sys.path.insert(0, _data_pipeline_root)

# 真实 27123 数据作为被跟踪的 fixtures 提交（data-pipeline/kbd/cache/ 被 .gitignore 忽略，
# CI 干净 checkout 不存在，故不能用它作为测试数据源）。CI 与本地均从此 fixtures 目录读取。
REAL_CACHE_27123 = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "fixtures", "27123")
)

# 27123 content 含 4 个 <img>，但首两个为同一 URL（去重后 3 张），
# 对应 cache 中的 img_0/1/2.png。
EXPECTED_UNIQUE_IMAGES = 3


def _copy_27123_cache(tmp_path: str) -> str:
    """将真实 cache/27123 复制到隔离的临时目录，避免污染 fixtures。"""
    dst = os.path.join(tmp_path, "27123")
    shutil.copytree(REAL_CACHE_27123, dst)
    return dst


def _collect_placeholders(result: dict) -> set[int]:
    """从全部结构化章节字段中收集出现的 ![img:N] 序号。"""
    text = "\n".join(
        str(result.get(f, ""))
        for f in (
            "problem_description", "alert_info", "steps_text", "root_cause",
            "solution", "operational_impact", "is_temporary", "recommendations",
        )
    )
    return set(int(m) for m in re.findall(r"!\[img:(\d+)\]", text))


class TestConvert27123Golden:
    """基于真实 27123 数据的端到端转换（.desc.txt 已被移除，cache 中无此类文件）。"""

    def test_real_cache_has_no_desc_txt(self):
        """清理锁定：真实 cache/27123 不得残留 .desc.txt（机制已彻底移除）。"""
        leftover = [f for f in os.listdir(REAL_CACHE_27123) if f.endswith(".desc.txt")]
        assert leftover == [], f"cache/27123 仍残留 .desc.txt: {leftover}"

    def test_golden_conversion(self, tmp_path):
        cache_dir = os.path.dirname(_copy_27123_cache(tmp_path))
        with patch("kbd.converter.settings.KBD_CACHE_DIR", Path(cache_dir)):
            from kbd.converter import convert_kbd_structured

            result = convert_kbd_structured("27123")

        assert result is not None, "27123 应转换成功（必填 section 齐全）"
        assert result["support_id"] == "27123"
        assert "虚拟机开机失败" in result["title"]

        # 8 大章节字段齐全
        for field in (
            "problem_description", "alert_info", "steps_text", "root_cause",
            "solution", "operational_impact", "is_temporary", "recommendations",
        ):
            assert field in result, f"缺少字段 {field}"

        # content_md 不在此生成，交由后端渲染
        assert result["content_md"] is None
        assert result["signals_json"] == []

        # images_json：3 张去重图片，seq 0/1/2，desc 恒为空（VISION 阶段填充）
        images_json = result["images_json"]
        assert len(images_json) == EXPECTED_UNIQUE_IMAGES
        assert [e["seq"] for e in images_json] == [0, 1, 2]
        for entry in images_json:
            assert entry["desc"] == "", "desc 初始必须为空，不能来自本地 .desc.txt"
            assert "context_before" in entry
            assert "context_after" in entry
            assert entry["section"] in (
                "problem_description", "alert_info", "steps_text", "root_cause",
                "solution", "operational_impact", "is_temporary", "recommendations",
            )
        assert any(
            entry["context_before"] or entry["context_after"]
            for entry in images_json
        ), "真实截图至少应有一侧章节上下文"

        # images（二进制）随 IMPORT 原子写入 kbd_image：3 张 png base64
        images = result["images"]
        assert len(images) == EXPECTED_UNIQUE_IMAGES
        for img in images:
            assert img["mime_type"] == "image/png"
            assert img["data_base64"], "图片 base64 不应为空"

        # 占位符覆盖全部去重图片
        assert _collect_placeholders(result) == {0, 1, 2}

        # 真实缓存中 4 个 <img> 但仅 3 张唯一 → 不应出现 seq >= 3
        all_seqs = set()
        for f in (
            "problem_description", "alert_info", "steps_text", "root_cause",
            "solution", "operational_impact", "is_temporary", "recommendations",
        ):
            all_seqs |= set(int(m) for m in re.findall(r"!\[img:(\d+)\]", str(result.get(f, ""))))
        assert max(all_seqs) < EXPECTED_UNIQUE_IMAGES


class TestConvert27123IgnoresDescTxt:
    """回归：即便 cache 中存在 img_N.desc.txt，转换器也必须忽略（desc 恒为空）。"""

    def test_desc_txt_is_ignored(self, tmp_path):
        cache_dir = os.path.dirname(_copy_27123_cache(tmp_path))
        # 故意写入伪造的 .desc.txt，模拟旧机制残留
        for seq in range(EXPECTED_UNIQUE_IMAGES):
            with open(os.path.join(cache_dir, "27123", f"img_{seq}.desc.txt"), "w", encoding="utf-8") as fh:
                fh.write("BACKGROUND: 伪造\nTYPE: 其他截图\nFULL_TEXT:\n- 不应被读取\n")

        with patch("kbd.converter.settings.KBD_CACHE_DIR", Path(cache_dir)):
            from kbd.converter import convert_kbd_structured

            result = convert_kbd_structured("27123")

        assert result is not None
        # 关键断言：.desc.txt 内容未被读取
        for entry in result["images_json"]:
            assert entry["desc"] == ""
        # 章节文本中也不能出现伪造描述内容
        full_text = "\n".join(str(result.get(f, "")) for f in (
            "problem_description", "alert_info", "steps_text", "root_cause",
            "solution", "operational_impact", "is_temporary", "recommendations",
        ))
        assert "不应被读取" not in full_text
        assert "伪造" not in full_text
