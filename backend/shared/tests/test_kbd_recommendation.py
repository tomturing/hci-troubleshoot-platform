"""纯文本知识与自动诊断的边界，以及标题召回的正反例。"""

from unittest.mock import AsyncMock

import pytest
from jsonschema import ValidationError
from shared.schemas.kbd_recommendation import recommend_cases
from shared.schemas.semantic_entry import capability_of
from shared.schemas.semantic_routing import resolve_candidates
from shared.schemas.signal_schema import validate_kbd_publishable_signals_json


def reference(identifier="1", title="安装 Windows Server 2016 提示缺少介质驱动程序", **fields):
    return {
        "id": identifier,
        "support_id": "15936",
        "title": title,
        "problem_description": "使用 ISO 安装 Windows Server 2016，提示缺少介质驱动程序",
        "root_cause": "历史案例根因",
        "solution": "历史案例处理方法",
        "signals_json": {"schema_version": 2, "signals": []},
        "resource_revision": {"revision": 3},
        "executable": False,
        **fields,
    }


def test_empty_document_is_publishable_reference_and_invalid_signal_still_fails():
    document = reference()["signals_json"]
    validate_kbd_publishable_signals_json(document)
    assert capability_of(document) == "reference_only"
    with pytest.raises(ValidationError):
        validate_kbd_publishable_signals_json({**document, "signals": [{"id": "bad"}]})
    with pytest.raises(ValidationError):
        validate_kbd_publishable_signals_json({**document, "semantic_entry_profile": {}})
    assert capability_of({**document, "rejected_candidates": [{"reason": "failed"}]}) is None
    assert capability_of({**document, "generation_metadata": {"status": "stale"}}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_applicable", "no_match", "matched_inconclusive", "source_unavailable"])
async def test_title_only_case_is_recommended_without_becoming_executable(status):
    result = await resolve_candidates(
        entries=[reference()],
        context="安装 Windows Server 2016 提示缺少介质驱动程序",
        strong_status=status,
        include_reference_cases=True,
    )
    assert result["decision"] == "case_recommendations"
    candidate = result["candidates"][0]
    assert candidate["kbd_id"] == "1"
    assert candidate["verified"] is False
    assert candidate["resource_revision"]["revision"] == 3
    assert candidate["recommendation_solution"] == "历史案例处理方法"
    assert result["strong_producer_status"] == status


@pytest.mark.asyncio
async def test_rejected_candidate_cannot_return_through_title_matching():
    result = await resolve_candidates(
        entries=[reference()],
        context="缺少介质驱动程序",
        strong_status="no_match",
        include_reference_cases=True,
        excluded_kbd_ids=["1"],
    )
    assert result["candidates"] == []
    assert result["decision"] != "case_recommendations"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["虚拟机问题", "没有出现缺少介质驱动程序", "以前缺少介质驱动程序；现在登录缓慢", "备份存储池满了"]
)
async def test_generic_negative_historical_and_unrelated_queries_do_not_force_a_match(query):
    assert await recommend_cases(entries=[reference()], context=query) is None


@pytest.mark.asyncio
async def test_root_cause_does_not_become_retrieval_input():
    entry = reference(root_cause="备份池磁盘空间不足", solution="清理备份池空间")
    assert await recommend_cases(entries=[entry], context="备份池磁盘空间不足") is None


@pytest.mark.asyncio
async def test_same_symptom_different_causes_remain_multiple_references():
    entries = [reference("1"), reference("2", root_cause="另一种原因")]
    result = await recommend_cases(entries=entries, context="缺少介质驱动程序")
    assert len(result["candidates"]) == 2
    assert result["ambiguous"] is True
    assert result["next_action"]["type"] == "compare_cases"
    assert all(candidate["verified"] is False for candidate in result["candidates"])


@pytest.mark.asyncio
async def test_vector_synonym_recall_and_outage_lexical_fallback():
    embed = AsyncMock(side_effect=[[[1.0, 0.0]], [[1.0, 0.0]]])
    result = await recommend_cases(entries=[reference()], context="系统部署过程报告找不到安装设备驱动", embed=embed)
    assert result["candidates"][0]["score_parts"]["vector"] == 1.0
    failed = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
    result = await recommend_cases(entries=[reference()], context="缺少介质驱动程序", embed=failed)
    assert result["degraded"] is True
    assert result["candidates"][0]["score_parts"]["vector"] == 0


@pytest.mark.asyncio
async def test_explicit_profile_exclusions_and_version_scope_are_retained():
    entry = reference()
    entry["signals_json"]["semantic_entry_profile"] = {
        "diagnosis_capability": "guidance_only",
        "canonical_symptoms": ["缺少介质驱动程序"],
        "positive_anchors": ["缺少介质驱动程序"],
        "exclusion_anchors": ["磁盘故障"],
        "applicability": {"product_version": ["6.*"]},
    }
    assert (
        await recommend_cases(entries=[entry], context={"description": "缺少介质驱动程序", "product_version": "7.0"})
        is None
    )
    assert await recommend_cases(entries=[entry], context="缺少介质驱动程序，同时磁盘故障") is None
    result = await recommend_cases(entries=[entry], context="缺少介质驱动程序")
    assert result["candidates"][0]["scope_warning"] == "missing_scope:product_version"


@pytest.mark.asyncio
async def test_recommendation_only_never_restarts_an_executable_semantic_candidate():
    entry = reference(executable=True)
    entry["signals_json"] = {
        "signals": [{"acquire": {"tool": "qkv_case_context"}}],
        "semantic_entry_profile": {
            "diagnosis_capability": "executable",
            "canonical_symptoms": ["缺少介质驱动程序"],
            "positive_anchors": ["缺少介质驱动程序"],
        },
    }
    result = await resolve_candidates(
        entries=[entry],
        context="缺少介质驱动程序",
        strong_status="not_applicable",
        include_reference_cases=True,
        recommendation_only=True,
    )
    assert result["decision"] == "case_recommendations"
    assert result["candidates"][0]["verified"] is False
