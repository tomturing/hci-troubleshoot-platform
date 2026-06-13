"""
Unit tests for StrictPromptLoader
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from shared.utils.prompt_loader import PromptLoadError, PromptValidationError, StrictPromptLoader
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_load_and_validate_success():
    db_session = MagicMock(spec=AsyncSession)

    # Mock execute: return "Hello {name}!"
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "Hello {name}!"
    db_session.execute = AsyncMock(return_value=mock_result)

    content = await StrictPromptLoader.load_and_validate(db_session, "test_prompt", ["name"])
    assert content == "Hello {name}!"


@pytest.mark.asyncio
async def test_load_and_validate_missing_placeholder():
    db_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "Hello World!"
    db_session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(PromptValidationError) as excinfo:
        await StrictPromptLoader.load_and_validate(db_session, "test_prompt", ["name"])
    assert "缺少运行时必需的占位符" in str(excinfo.value)


@pytest.mark.asyncio
async def test_load_and_validate_redundant_placeholder():
    db_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "Hello {name}! Age {age}."
    db_session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(PromptValidationError) as excinfo:
        await StrictPromptLoader.load_and_validate(db_session, "test_prompt", ["name"])
    assert "包含运行时无法识别的非法占位符" in str(excinfo.value)


@pytest.mark.asyncio
async def test_load_and_validate_db_error():
    db_session = MagicMock(spec=AsyncSession)
    db_session.execute = AsyncMock(side_effect=Exception("Connection refused"))

    with pytest.raises(PromptLoadError) as excinfo:
        await StrictPromptLoader.load_and_validate(db_session, "test_prompt", ["name"])
    assert "数据库查询异常" in str(excinfo.value)


@pytest.mark.asyncio
async def test_load_and_validate_not_found():
    db_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db_session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(PromptLoadError) as excinfo:
        await StrictPromptLoader.load_and_validate(db_session, "test_prompt", ["name"])
    assert "未找到处于激活状态且名称为" in str(excinfo.value)


@pytest.mark.asyncio
async def test_load_and_validate_resolves_prompt_slot(monkeypatch):
    from shared.dynamic_resource.models import ResourceSnapshot
    from shared.utils import prompt_loader

    slot = MagicMock()
    slot.slot_name = "sop_react_new"
    slot.active_prompt_name = "s1_sop_react_new_v2"
    slot.expected_placeholders = ["sop_title"]
    slot.consumer = "agent-service.investigation_agent"

    prompt = MagicMock()
    prompt.name = "s1_sop_react_new_v2"
    prompt.stage = "S1"
    prompt.description = "SOP 导航"
    prompt.content_template = "当前 SOP：{sop_title}"
    prompt.version = "2.0"
    prompt.is_active = True

    slot_result = MagicMock()
    slot_result.scalar_one_or_none.return_value = slot
    prompt_result = MagicMock()
    prompt_result.scalar_one_or_none.return_value = prompt

    db_session = MagicMock(spec=AsyncSession)
    db_session.execute = AsyncMock(side_effect=[slot_result, prompt_result])
    db_session.commit = AsyncMock()

    published = []

    async def fake_ensure_published(self, **kwargs):
        published.append(kwargs)
        return ResourceSnapshot(
            resource_type=kwargs["resource_type"],
            resource_name=kwargs["resource_name"],
            revision=len(published),
            version=kwargs["version"],
            status=kwargs["status"],
            content=kwargs["content"],
            contract=kwargs["contract"],
            dependencies=kwargs["dependencies"],
            checksum=f"checksum-{len(published)}",
        )

    audit_calls = []

    async def fake_audit_usage(self, snapshot, usage):
        audit_calls.append((snapshot, usage))

    monkeypatch.setattr(prompt_loader.DynamicResourcePublisher, "ensure_published", fake_ensure_published)
    monkeypatch.setattr(prompt_loader.DynamicResourceLoader, "audit_usage", fake_audit_usage)

    content = await StrictPromptLoader.load_and_validate(
        db_session,
        "sop_react_new",
        ["legacy_placeholder"],
        consumer="test-consumer",
        conversation_id="conv-1",
        case_id="case-1",
        trace_id="trace-1",
    )

    assert content == "当前 SOP：{sop_title}"
    assert [item["resource_type"] for item in published] == ["prompt_slot", "prompt"]
    assert published[0]["dependencies"] == [{"resource_type": "prompt", "resource_name": "s1_sop_react_new_v2"}]
    assert len(audit_calls) == 2
    assert audit_calls[0][1].consumer == "test-consumer"
    db_session.commit.assert_awaited_once()
