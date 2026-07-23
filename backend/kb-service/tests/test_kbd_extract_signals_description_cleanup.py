from __future__ import annotations

from app.routes.extract_signals import (
    _clean_signal_description,
    _enrich_signal,
    _looks_descriptive,
)


def _base_signal(resource_keyword: str | None = None, description: str | None = None) -> dict:
    """构造一条最小合法的 qfk_storage v2 信号，便于聚焦测试说明/关键字纠错。"""
    args: dict = {"sub_command": "list"}
    if resource_keyword is not None:
        args["resource_keyword"] = resource_keyword
    if description is not None:
        args["description"] = description
    return {
        "id": "sig_x",
        "acquire": {"tool": "qfk_storage", "args": args},
        "match": {"type": "keyword", "pattern": "镜像占用", "mode": "any", "expected": True},
        "orchestrate": {"produces": [], "requires": ["HOST"]},
        "provenance": {
            "category": "backend",
            "source_section": "steps_text",
            "evidence": "检查镜像文件占用情况",
            "confidence": 0.8,
        },
        "review": {"require_human_confirm": False, "notes": ""},
    }


def test_description_relocated_from_resource_keyword():
    """复现并修复：说明错填进 resource_keyword（UI 的"关键字"字段）时迁回 description。"""
    sig = _base_signal(resource_keyword="镜像文件占用检查")
    _clean_signal_description(sig)
    args = sig["acquire"]["args"]
    assert args.get("description") == "镜像文件占用检查"
    assert "resource_keyword" not in args


def test_no_migration_when_description_present_and_real_keyword():
    """description 已正确填写、resource_keyword 是真实标识符时不迁移。"""
    sig = _base_signal(resource_keyword="vgpu", description="镜像文件占用检查")
    _clean_signal_description(sig)
    args = sig["acquire"]["args"]
    assert args["description"] == "镜像文件占用检查"
    assert args["resource_keyword"] == "vgpu"


def test_no_false_positive_for_real_identifier_without_description():
    """resource_keyword 为真实资源标识符（无 description）时不得误判为说明。"""
    sig = _base_signal(resource_keyword="vgpu")
    _clean_signal_description(sig)
    assert "description" not in sig["acquire"]["args"]
    assert sig["acquire"]["args"]["resource_keyword"] == "vgpu"


def test_existing_description_not_overwritten_by_descriptive_keyword():
    """description 已存在时，即便 resource_keyword 像说明也不覆盖原有说明。"""
    sig = _base_signal(resource_keyword="镜像文件占用检查", description="磁盘占用检查")
    _clean_signal_description(sig)
    args = sig["acquire"]["args"]
    assert args["description"] == "磁盘占用检查"
    assert args["resource_keyword"] == "镜像文件占用检查"


def test_no_side_effect_without_resource_keyword():
    """无 resource_keyword 的合法信号不产生副作用。"""
    sig = _base_signal(description="镜像文件占用检查")
    _clean_signal_description(sig)
    assert "resource_keyword" not in sig["acquire"]["args"]


def test_looks_descriptive_helper():
    assert _looks_descriptive("镜像文件占用检查")
    assert not _looks_descriptive("vgpu")
    assert not _looks_descriptive("")
    assert not _looks_descriptive(None)


def test_cleaner_wired_into_enrich_signal():
    """集成校验：经 _enrich_signal 入口，错填的 resource_keyword 应被纠正为 description。"""
    sig = _base_signal(resource_keyword="镜像文件占用检查")
    out = _enrich_signal(sig)
    args = out["acquire"]["args"]
    assert args.get("description") == "镜像文件占用检查"
    assert "resource_keyword" not in args


def test_match_pattern_relocated_when_descriptive_long_sentence():
    """复现并修复：说明性长句错填进 match.pattern（QFK 的"关键字"字段）时迁回 description。"""
    sig = {
        "id": "sig_y",
        "acquire": {"tool": "qfk_system", "args": {"sub_command": "lsof"}},
        "match": {"type": "keyword", "pattern": "镜像文件占用检查", "mode": "any", "expected": True},
        "orchestrate": {"produces": [], "requires": ["HOST"]},
        "provenance": {
            "category": "backend",
            "source_section": "steps_text",
            "evidence": "检查镜像文件占用情况",
            "confidence": 0.8,
        },
        "review": {"require_human_confirm": False, "notes": ""},
    }
    _clean_signal_description(sig)
    args = sig["acquire"]["args"]
    assert args.get("description") == "镜像文件占用检查"
    assert sig["match"]["pattern"] == ""
    assert sig.get("provenance", {}).get("needs_review") is True


def test_cleaner_wired_into_enrich_signal_for_pattern():
    """集成校验：经 _enrich_signal 入口，错填进 match.pattern 的说明应迁 description 并标 needs_review。"""
    sig = {
        "id": "sig_z",
        "acquire": {"tool": "qfk_system", "args": {"sub_command": "ps"}},
        "match": {"type": "keyword", "pattern": "第三方进程确认", "mode": "any", "expected": True},
        "orchestrate": {"produces": [], "requires": ["HOST"]},
        "provenance": {
            "category": "backend",
            "source_section": "steps_text",
            "evidence": "确认第三方进程",
            "confidence": 0.8,
        },
        "review": {"require_human_confirm": False, "notes": ""},
    }
    out = _enrich_signal(sig)
    args = out["acquire"]["args"]
    assert args.get("description") == "第三方进程确认"
    assert out["match"]["pattern"] == ""
    assert out.get("provenance", {}).get("needs_review") is True
