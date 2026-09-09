"""共享语义路由的正例、反例、安全边界及可重建向量缓存回归。"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jsonschema import ValidationError
from shared.schemas.semantic_entry import (
    normalize_case_context,
    propose_semantic_profile,
    validate_profile_source_evidence,
    validate_semantic_entry_profile,
)
from shared.schemas.semantic_routing import _VECTOR_CACHE, resolve_candidates


def entry(identifier="1", capability="executable", **profile):
    return {
        "id": identifier,
        "support_id": identifier,
        "title": "测试知识",
        "resource_revision": {"revision": 1},
        "signals_json": {
            "signals": [{"id": "context", "acquire": {"tool": "qkv_case_context", "args": {}}}],
            "semantic_entry_profile": {
                "schema_version": 1,
                "diagnosis_capability": capability,
                "canonical_symptoms": ["创建虚拟机时镜像格式不支持"],
                "positive_anchors": ["镜像格式不支持"],
                "exclusion_anchors": ["空间不足"],
                "manual_evidence_request": ["请补充完整报错"],
                **profile,
            },
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        "创建虚拟机时镜像格式不支持",
        "背景" * 500 + "创建虚拟机时镜像格式不支持",
        {"description": "创建虚拟机失败", "error_text": "镜像格式不支持"},
        "没有空间不足报错，当前镜像格式不支持",
        "以前空间不足；当前镜像格式不支持",
    ],
)
async def test_explicit_current_error_survives_long_context_and_form(context):
    result = await resolve_candidates(entries=[entry()], context=context, strong_status="not_applicable")
    assert result["decision"] == "executable"
    assert result["candidates"][0]["kbd_id"] == "1"
    assert not result["filtered_candidates"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "reason"),
    [
        ("镜像格式不支持且空间不足", "exclusion_anchor_hit"),
        ("没有镜像格式不支持报错", "positive_anchor_missing"),
        ("没有出现“镜像格式不支持”，只是启动很慢", "positive_anchor_missing"),
        ("以前“镜像格式不支持”；现在登录失败", "positive_anchor_missing"),
        ("以前镜像格式不支持；现在登录失败", "positive_anchor_missing"),
    ],
)
async def test_negative_and_historical_context_cannot_create_current_evidence(context, reason):
    result = await resolve_candidates(entries=[entry()], context=context, strong_status="no_match")
    assert result["decision"] == "inconclusive"
    assert result["filtered_candidates"][0]["reason"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["matched", "source_unavailable", "unknown"])
async def test_strong_source_guard_does_not_call_vector_service(status):
    embed = AsyncMock()
    result = await resolve_candidates(entries=[entry()], context="镜像格式不支持", strong_status=status, embed=embed)
    assert result["decision"] == "strong_producer_first"
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_not_applicable_is_checked_against_authoritative_inventory():
    strong = {"id": "strong", "signals_json": {"signals": [{"acquire": {"tool": "qkv_task"}}]}}
    result = await resolve_candidates(
        entries=[entry(), strong], context="镜像格式不支持", strong_status="not_applicable"
    )
    assert result["decision"] == "strong_producer_first"


@pytest.mark.asyncio
async def test_guidance_gap_and_ambiguous_candidates_never_execute():
    for capability in ("guidance_only", "capability_gap"):
        result = await resolve_candidates(
            entries=[entry(capability=capability)], context="镜像格式不支持", strong_status="no_match"
        )
        assert result["decision"] == "inconclusive"
        assert result["next_action"]["question"]
    result = await resolve_candidates(
        entries=[entry(str(i)) for i in range(4)], context="镜像格式不支持", strong_status="no_match", top_k=1
    )
    assert result["decision"] == "inconclusive"
    assert result["candidate_count"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "expected"), [("6.12.0", "executable"), ("6.9", "inconclusive"), ("", "inconclusive")]
)
async def test_version_scope_is_hard_gate(version, expected):
    result = await resolve_candidates(
        entries=[entry(applicability={"product_version": [">=6.12"]})],
        context={"description": "镜像格式不支持", "product_version": version},
        strong_status="no_match",
    )
    assert result["decision"] == expected


@pytest.mark.asyncio
async def test_version_range_and_missing_scope_question():
    document = entry(applicability={"product_version": [">=6.10,<6.13"], "component": ["存储"]})
    result = await resolve_candidates(
        entries=[document],
        context={"description": "镜像格式不支持", "product_version": "6.12.0"},
        strong_status="no_match",
    )
    assert result["decision"] == "inconclusive"
    assert "组件" in result["next_action"]["question"]
    assert result["filtered_candidates"][0]["reason_text"] == "尚未提供组件"
    result = await resolve_candidates(
        entries=[document],
        context={"description": "镜像格式不支持", "product_version": "6.12.0", "component": "存储"},
        strong_status="no_match",
    )
    assert result["decision"] == "executable"


def test_explicit_error_keeps_up_to_one_thousand_chars():
    assert len(normalize_case_context({"description": "异常", "error_text": "X" * 1000})["error_text"]) == 1000


@pytest.mark.asyncio
async def test_vector_cache_uses_model_and_profile_digest_and_degrades_entire_batch():
    _VECTOR_CACHE.clear()
    embed = AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for text in texts])
    await resolve_candidates(
        entries=[entry()],
        context="镜像格式不支持",
        strong_status="no_match",
        embed=embed,
        embedding_namespace="test-model",
    )
    assert embed.await_count == 2
    await resolve_candidates(
        entries=[entry()],
        context="镜像格式不支持",
        strong_status="no_match",
        embed=embed,
        embedding_namespace="test-model",
    )
    assert embed.await_count == 3
    await resolve_candidates(
        entries=[entry(canonical_symptoms=["更新后的症状"])],
        context="镜像格式不支持",
        strong_status="no_match",
        embed=embed,
        embedding_namespace="test-model",
    )
    assert embed.await_count == 5
    failed = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
    result = await resolve_candidates(
        entries=[entry()], context="镜像格式不支持", strong_status="no_match", embed=failed
    )
    assert result["degraded"] is True
    assert result["candidates"][0]["score_parts"] == {"anchor_score": result["candidates"][0]["score"]}


@pytest.mark.asyncio
async def test_large_category_reranking_is_bounded_without_hiding_ambiguity():
    embed = AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts])
    result = await resolve_candidates(
        entries=[entry(str(i)) for i in range(100)], context="镜像格式不支持", strong_status="no_match", embed=embed
    )
    assert embed.await_count <= 11
    assert result["candidate_count"] == 100
    assert result["decision"] == "inconclusive"


@pytest.mark.asyncio
async def test_input_secrets_are_removed_before_embedding_and_observation():
    embed = AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for text in texts])
    result = await resolve_candidates(
        entries=[entry()], context="镜像格式不支持 password=example-secret", strong_status="no_match", embed=embed
    )
    assert "example-secret" not in str(embed.await_args_list)
    assert "example-secret" not in str(result)


def test_pipeline_profile_is_grounded_and_does_not_fabricate_consumer():
    source = {
        "problem_description": "创建虚拟机提示“镜像格式不支持”",
        "root_cause": "SECRET_CAUSE",
        "solution": "rm -rf",
    }
    profile = propose_semantic_profile(source, [])
    assert profile["diagnosis_capability"] == "guidance_only"
    assert profile["positive_anchors"] == ["镜像格式不支持"]
    assert "SECRET_CAUSE" not in str(profile) and "rm -rf" not in str(profile)
    assert propose_semantic_profile({"problem_description": "删除文件可以解决"}, []) is None
    assert "HOST" not in normalize_case_context(source["problem_description"])
    document = {"semantic_entry_profile": profile, "signals": [{"acquire": {"tool": "qkv_case_context"}}]}
    validate_semantic_entry_profile(document)
    validate_profile_source_evidence(document, source)
    assert profile["source_evidence"][0]["source_ref"] == "problem_description"
    with pytest.raises(ValidationError, match="已过期"):
        validate_profile_source_evidence(document, {**source, "problem_description": "发生了另一个问题"})
    profile["positive_anchors"] = ["一个不存在的错误"]
    with pytest.raises(ValidationError, match="正反例"):
        validate_semantic_entry_profile(document)


@pytest.mark.asyncio
async def test_hci_sim_semantic_scenarios_drive_online_and_offline_routing():
    """hci-sim 的四类声明必须执行共享路由，而不是只校验 JSON 键存在。"""

    fixture = Path(__file__).resolve().parents[3] / "hci_sim/testdata/sample-suites/diagnosis-signal-matrix-v1.json"
    scenarios = json.loads(fixture.read_text(encoding="utf-8"))["semantic_entry_scenarios"]
    for name, scenario in scenarios.items():
        candidate = scenario["candidate"]
        entry = {
            "id": scenario["case_id"],
            "support_id": scenario["case_id"],
            "title": name,
            "executable": candidate["executable"],
            "signals_json": {
                "signals": [{"id": "case_context", "acquire": {"tool": "qkv_case_context", "args": {}}}],
                "semantic_entry_profile": {
                    "schema_version": 1,
                    **{key: value for key, value in candidate.items() if key != "executable"},
                },
            },
        }
        for mode, expected_key in (("online", "expected_online_decision"), ("offline", "expected_offline_decision")):
            result = await resolve_candidates(
                entries=[entry],
                context=scenario["case_context"],
                strong_status=scenario["strong_producer_status"],
                mode=mode,
            )
            assert result["decision"] == scenario[expected_key], (name, mode, result)
            assert result["reason"] == scenario["expected_online_reason"], (name, mode, result)
            if scenario[expected_key] == "executable":
                assert result["candidates"][0]["support_id"] == scenario["case_id"]
            if scenario.get("expected_manual_evidence_request"):
                assert result["next_action"]["question"] == candidate["manual_evidence_request"][0]
