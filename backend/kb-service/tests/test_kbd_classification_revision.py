"""KBD 分类 Proposal 的生成指纹与统一 revision 入库契约。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app.routes.admin as admin
import app.routes.classify as classify
import pytest


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


def test_classify_model_ignores_empty_dedicated_setting(monkeypatch):
    monkeypatch.setenv("CLASSIFY_MODEL", "")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "deepseek-v4-flash")

    assert classify._resolve_classify_model() == "deepseek-v4-flash"


def test_parse_llm_response_sorts_deduplicates_and_uses_winner_reason():
    result = classify.parse_llm_response(
        {
            "top3": [
                {"category_id": "vm-002", "label": "状态异常", "score": 0.4, "reason": "次选"},
                {"category_id": "vm-001", "label": "启动失败", "score": 0.9, "reason": "最高分理由"},
                {"category_id": "vm-001", "label": "重复低分", "score": 0.2, "reason": "重复项"},
            ]
        },
        {"vm-001", "vm-002"},
    )

    assert result.category_id == "vm-001"
    assert result.confidence == 0.9
    assert result.reason == "最高分理由"
    assert [item.category_id for item in result.top3] == ["vm-001", "vm-002"]


def test_parse_llm_response_rejects_when_all_candidates_are_invalid():
    with pytest.raises(classify.HTTPException) as exc_info:
        classify.parse_llm_response(
            {
                "top3": [
                    {"category_id": "not-exists", "label": "伪造分类", "score": 0.9},
                    {"category_id": "vm-001", "label": "非法分数", "score": "NaN"},
                ]
            },
            {"vm-001"},
        )

    assert exc_info.value.status_code == 502
    assert "合法分类候选" in exc_info.value.detail


def test_parse_llm_response_rejects_non_array_top3():
    with pytest.raises(classify.HTTPException) as exc_info:
        classify.parse_llm_response({"top3": 3}, {"vm-001"})

    assert exc_info.value.status_code == 502
    assert "top3 分类数组" in exc_info.value.detail


@pytest.mark.asyncio
async def test_classify_case_builds_model_prompt_catalog_and_input_fingerprints():
    session = AsyncMock()
    db = MagicMock()
    db.async_session_factory.return_value = _SessionContext(session)
    categories = [{"code": "vm-001", "name": "启动失败", "domain": "虚拟机", "path": ["虚拟机", "启动"]}]
    llm_result = {
        "top3": [{"category_id": "vm-001", "label": "启动失败", "score": 0.9, "reason": "命中启动失败"}]
    }

    with (
        patch.object(classify, "fetch_categories_for_classify", AsyncMock(return_value=categories)),
        patch.object(
            classify.StrictPromptLoader,
            "load_and_validate",
            AsyncMock(return_value="{count}\n{categories_text}\n{title}\n{problem_desc}"),
        ),
        patch.object(classify, "call_llm", AsyncMock(return_value=llm_result)),
    ):
        response = await classify.classify_case(db, "虚拟机启动失败", "任务提示镜像被占用")

    metadata = response.generation_metadata
    assert metadata["generation_kind"] == "classification"
    assert metadata["model_id"] == classify.LLM_MODEL
    assert metadata["prompt_name"] == "kbd_classify_v1"
    assert len(metadata["prompt_revision"]) == 64
    assert len(metadata["category_catalog_revision"]) == 64
    assert len(metadata["input_hash"]) == 64


@pytest.mark.asyncio
async def test_reclassify_freezes_every_ai_result_with_shared_revision_service():
    read_session = AsyncMock()
    write_session = AsyncMock()
    contexts = iter((_SessionContext(read_session), _SessionContext(write_session)))
    db = MagicMock()
    db.async_session_factory.side_effect = lambda: next(contexts)
    entry = SimpleNamespace(
        id=9,
        title="虚拟机启动失败",
        problem_description="任务提示镜像被占用",
        category_id=None,
        ai_category_id="vm-old",
        ai_category_conf=0.4,
        ai_category_reason="旧结果",
        signals_json={"schema_version": 2, "signals": []},
        latest_proposal_revision_id=7,
        working_revision_id=8,
    )
    response = classify.ClassifyResponse(
        category_id="vm-001",
        confidence=0.9,
        reason="命中启动失败",
        top3=[classify.Top3Item(category_id="vm-001", label="启动失败", score=0.9)],
        needs_review=False,
        generation_metadata={"generation_kind": "classification", "model_id": "model-a"},
    )
    created = SimpleNamespace(id=10)

    async def require_entry(_session, _kbd_id, *, for_update=False):
        assert _kbd_id == 9
        return entry

    with (
        patch.object(admin, "_check_auth"),
        patch.object(admin, "_db_manager", db),
        patch.object(admin, "_require_directly_mutable_kbd", require_entry),
        patch.object(classify, "classify_case", AsyncMock(return_value=response)),
        patch.object(admin, "freeze_kbd_ai_proposal", AsyncMock(return_value=created)) as ensure,
    ):
        result = await admin.reclassify_kbd_entry(MagicMock(), 9)

    assert result["proposal_revision_id"] == 10
    assert entry.ai_category_id == "vm-001"
    kwargs = ensure.await_args.kwargs
    assert kwargs["generation_kind"] == "classification"
    assert kwargs["origin"] == "category_reclassify"
    assert kwargs["generation_metadata"]["generation_kind"] == "classification"
    write_session.commit.assert_awaited_once()
