"""
SOP Matcher 单元测试

测试关键字精确匹配逻辑，不依赖数据库或外部服务。
"""

import json
from pathlib import Path

import pytest
from app.services.sop_matcher import SopMatcher


@pytest.fixture
def sop_skills_dir(tmp_path: Path) -> Path:
    """创建临时 SOP 测试目录"""
    # 创建 vm_boot_failure 技能目录
    skill_dir = tmp_path / "vm_boot_failure"
    chapters_dir = skill_dir / "chapters"
    chapters_dir.mkdir(parents=True)

    # 创建 keywords_map.json
    keywords_map = {
        "CPU不足": {
            "keywords": ["CPU不足", "剩余可配置CPU不足", "此主机剩余可配置CPU不足"],
            "file": "04_CPU不足.md",
        },
        "内部异常": {
            "keywords": ["内部异常", "内部异常，请稍后重试"],
            "file": "06_内部异常.md",
            "subchapters": {
                "KVM驱动缺失": {
                    "keywords": ["failed to initialize KVM", "kvm: disable by bios", "kvm_intel"],
                    "file": "03_内部异常/0302_KVM驱动缺失.md",
                }
            },
        },
    }
    (skill_dir / "keywords_map.json").write_text(json.dumps(keywords_map, ensure_ascii=False), encoding="utf-8")

    # 创建章节文件
    (chapters_dir / "04_CPU不足.md").write_text(
        "# CPU 不足排障\n\n检查步骤：\n1. 查看 CPU 剩余配额\n2. 迁移虚拟机",
        encoding="utf-8",
    )

    return tmp_path


@pytest.mark.asyncio
async def test_sop_match_keyword(sop_skills_dir: Path):
    """测试关键字精确匹配"""
    matcher = SopMatcher(str(sop_skills_dir))
    await matcher.load()

    # 精确匹配
    result = matcher.match("虚拟机开机失败，提示 CPU不足")
    assert result is not None
    assert result.skill_id == "vm_boot_failure"
    assert result.node_name == "CPU不足"
    assert result.matched_keyword == "cpu不足"  # 大小写归一化


@pytest.mark.asyncio
async def test_sop_match_subchapter(sop_skills_dir: Path):
    """测试子章节关键字匹配（精度更高）"""
    matcher = SopMatcher(str(sop_skills_dir))
    await matcher.load()

    result = matcher.match("开机报错 failed to initialize KVM")
    assert result is not None
    assert result.node_name == "KVM驱动缺失"
    assert "kvm" in result.matched_keyword


@pytest.mark.asyncio
async def test_sop_no_match(sop_skills_dir: Path):
    """无关键字时返回 None"""
    matcher = SopMatcher(str(sop_skills_dir))
    await matcher.load()

    result = matcher.match("虚拟机迁移失败，网络不通")
    assert result is None


@pytest.mark.asyncio
async def test_sop_longest_match(sop_skills_dir: Path):
    """最长匹配优先"""
    matcher = SopMatcher(str(sop_skills_dir))
    await matcher.load()

    # "此主机剩余可配置CPU不足" 比 "CPU不足" 更长，应优先命中
    result = matcher.match("此主机剩余可配置CPU不足，请迁移虚拟机")
    assert result is not None
    assert result.matched_keyword == "此主机剩余可配置cpu不足"


@pytest.mark.asyncio
async def test_sop_empty_dir():
    """空目录不会抛出异常"""
    matcher = SopMatcher("/tmp/nonexistent_sop_dir_12345")
    await matcher.load()   # 不应抛出异常
    assert matcher.index_size == 0


@pytest.mark.asyncio
async def test_sop_index_size(sop_skills_dir: Path):
    """加载后索引大小正确"""
    matcher = SopMatcher(str(sop_skills_dir))
    await matcher.load()

    # cpu不足(3) + 内部异常(2) + kvm子章节(3) = 8 个关键字
    assert matcher.index_size == 8
