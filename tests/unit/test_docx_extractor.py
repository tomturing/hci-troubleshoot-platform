"""
DocxExtractor 单元测试

验收标准：
  1. atoms >= 25 个
  2. 错误码 >= 3 个
  3. CPU不足 / 内存不足 / 序列号过期 各有至少一个 diagnostic_step + fix_action 原子
  4. CPU不足 的 diagnostic_step 原子包含 acli 命令
  5. 所有原子的 trigger.task_error_keywords 非空
  6. 命令去重：每个原子内命令不重复
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 允许直接从项目根运行测试
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "data-pipeline"))

from atoms.docx_extractor import DocxExtractor, KnowledgeAtomDraft

DOCX_PATH = ROOT / "data-pipeline/sop_skills/虚拟机开关机失败排障手册.docx"


@pytest.fixture(scope="module")
def extraction_result():
    """模块级一次性执行提取（避免重复解析 docx）"""
    if not DOCX_PATH.exists():
        pytest.skip(f"docx 文件不存在：{DOCX_PATH}")
    extractor = DocxExtractor(DOCX_PATH)
    atoms, error_code_index = extractor.extract()
    return atoms, error_code_index


# ─────────────────────────────────────────────────────────────────────────────
# 验收测试
# ─────────────────────────────────────────────────────────────────────────────


class TestAtomCount:
    def test_atom_count_meets_minimum(self, extraction_result):
        """原子总数 >= 25"""
        atoms, _ = extraction_result
        assert len(atoms) >= 25, f"原子数量不足：{len(atoms)}"

    def test_has_all_three_types(self, extraction_result):
        """包含 background / diagnostic_step / fix_action 三种类型"""
        atoms, _ = extraction_result
        types = {a.type for a in atoms}
        assert "background" in types, "缺少 background 类型原子"
        assert "diagnostic_step" in types, "缺少 diagnostic_step 类型原子"
        assert "fix_action" in types, "缺少 fix_action 类型原子"

    def test_diagnostic_step_matches_fix_action_count(self, extraction_result):
        """diagnostic_step 和 fix_action 数量基本平衡（误差 <= 10）"""
        atoms, _ = extraction_result
        ds = sum(1 for a in atoms if a.type == "diagnostic_step")
        fa = sum(1 for a in atoms if a.type == "fix_action")
        assert abs(ds - fa) <= 10, f"类型分布失衡：diagnostic_step={ds}, fix_action={fa}"


class TestErrorCodeIndex:
    def test_error_code_count_meets_minimum(self, extraction_result):
        """错误码数量 >= 3"""
        _, ec_index = extraction_result
        assert len(ec_index) >= 3, f"错误码不足：{len(ec_index)}"

    def test_error_codes_uppercase(self, extraction_result):
        """所有错误码存储为大写格式"""
        _, ec_index = extraction_result
        for ec in ec_index:
            assert ec == ec.upper(), f"错误码未大写：{ec}"

    def test_error_codes_have_atom_ids(self, extraction_result):
        """每个错误码至少关联 1 个原子 ID"""
        atoms, ec_index = extraction_result
        atom_ids = {a.id for a in atoms}
        for ec, ids in ec_index.items():
            assert len(ids) >= 1, f"错误码 {ec} 无关联原子"
            for aid in ids:
                assert aid in atom_ids, f"错误码 {ec} 关联了不存在的原子 ID {aid}"


class TestKeyScenarios:
    """CPU不足 / 内存不足 / 序列号过期 三个场景的验收"""

    @pytest.mark.parametrize("kw", ["CPU不足", "内存不足", "序列号过期"])
    def test_scenario_has_diagnostic_step(self, extraction_result, kw):
        """关键错误类型有 diagnostic_step 原子"""
        atoms, _ = extraction_result
        matching = [
            a for a in atoms
            if kw in a.trigger.get("task_error_keywords", []) and a.type == "diagnostic_step"
        ]
        assert len(matching) >= 1, f"{kw} 缺少 diagnostic_step 原子"

    @pytest.mark.parametrize("kw", ["CPU不足", "内存不足", "序列号过期"])
    def test_scenario_has_fix_action(self, extraction_result, kw):
        """关键错误类型有 fix_action 原子"""
        atoms, _ = extraction_result
        matching = [
            a for a in atoms
            if kw in a.trigger.get("task_error_keywords", []) and a.type == "fix_action"
        ]
        assert len(matching) >= 1, f"{kw} 缺少 fix_action 原子"

    def test_cpu_diagnostic_has_acli_command(self, extraction_result):
        """CPU不足 的 diagnostic_step 原子含 acli 命令"""
        atoms, _ = extraction_result
        matching = [
            a for a in atoms
            if "CPU不足" in a.trigger.get("task_error_keywords", [])
            and a.type == "diagnostic_step"
            and any("acli" in cmd for cmd in a.content.get("commands", []))
        ]
        assert len(matching) >= 1, "CPU不足 diagnostic_step 原子中未发现 acli 命令"


class TestAtomStructure:
    def test_all_atoms_have_non_empty_keywords(self, extraction_result):
        """所有原子的 trigger.task_error_keywords 非空"""
        atoms, _ = extraction_result
        for atom in atoms:
            kws = atom.trigger.get("task_error_keywords", [])
            assert len(kws) >= 1, f"原子 {atom.id} 的触发关键字为空"

    def test_all_atoms_category_id_set(self, extraction_result):
        """所有原子的 category_id 已设置"""
        atoms, _ = extraction_result
        for atom in atoms:
            assert atom.category_id, f"原子 {atom.id} 的 category_id 未设置"

    def test_all_atoms_have_full_text(self, extraction_result):
        """所有原子的 content.full_text 非空"""
        atoms, _ = extraction_result
        for atom in atoms:
            full_text = atom.content.get("full_text", "")
            assert full_text.strip(), f"原子 {atom.id} 的 full_text 为空"

    def test_commands_deduplicated(self, extraction_result):
        """每个原子内命令不重复"""
        atoms, _ = extraction_result
        for atom in atoms:
            cmds = atom.content.get("commands", [])
            assert len(cmds) == len(set(cmds)), f"原子 {atom.id} 存在重复命令: {cmds}"

    def test_atom_ids_unique(self, extraction_result):
        """所有原子 ID 唯一"""
        atoms, _ = extraction_result
        ids = [a.id for a in atoms]
        assert len(ids) == len(set(ids)), "存在重复的原子 ID"

    def test_confidence_range(self, extraction_result):
        """置信度 0.0-1.0"""
        atoms, _ = extraction_result
        for atom in atoms:
            assert 0.0 <= atom.confidence <= 1.0, f"原子 {atom.id} 置信度越界: {atom.confidence}"


# ─────────────────────────────────────────────────────────────────────────────
# 命令提取单元测试（不依赖 docx 文件）
# ─────────────────────────────────────────────────────────────────────────────


class TestCommandExtraction:
    def test_backtick_extraction(self):
        """反引号包裹命令提取"""
        cmds = DocxExtractor._extract_commands([
            "执行 `acli vm.on name=vm1` 开机",
            "查看 `systemctl status vtpdaemon`",
        ])
        assert "acli vm.on name=vm1" in cmds
        assert "systemctl status vtpdaemon" in cmds

    def test_inline_acli_extraction(self):
        """行内嵌入 acli 命令提取"""
        cmds = DocxExtractor._extract_commands([
            "命令一：使用  acli system top  检查是否有占CPU核数较多的资源",
            "使用 acli system ps auxf | grep ${PID} 查看进程",
        ])
        assert any("acli system top" in c for c in cmds)
        assert any("acli system ps auxf" in c for c in cmds)

    def test_no_duplicates(self):
        """相同命令不重复"""
        cmds = DocxExtractor._extract_commands([
            "使用 `acli vm.on` 开机",
            "再次使用 `acli vm.on` 确认",
        ])
        assert cmds.count("acli vm.on") == 1

    def test_error_code_extraction(self):
        """错误码提取"""
        text = "报错描述：获取版本号失败，错误码：0x010032F5"
        codes = DocxExtractor._extract_error_codes(text)
        assert "0x010032F5" in codes

    def test_error_code_extraction_multiple(self):
        """多个错误码提取"""
        text = "两个错误码：0x010032F5 和 0x0CFFFFFF"
        codes = DocxExtractor._extract_error_codes(text)
        assert len(codes) == 2
        assert "0x010032F5" in codes
        assert "0x0CFFFFFF" in codes

    def test_error_code_dedup(self):
        """相同错误码去重"""
        text = "0x010032F5 出现两次 0x010032F5"
        codes = DocxExtractor._extract_error_codes(text)
        assert codes.count("0x010032F5") == 1
