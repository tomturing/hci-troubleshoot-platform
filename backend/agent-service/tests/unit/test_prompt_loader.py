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

    content = await StrictPromptLoader.load_and_validate(
        db_session, "test_prompt", ["name"]
    )
    assert content == "Hello {name}!"

@pytest.mark.asyncio
async def test_load_and_validate_missing_placeholder():
    db_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "Hello World!"
    db_session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(PromptValidationError) as excinfo:
        await StrictPromptLoader.load_and_validate(
            db_session, "test_prompt", ["name"]
        )
    assert "缺少运行时必需的占位符" in str(excinfo.value)

@pytest.mark.asyncio
async def test_load_and_validate_redundant_placeholder():
    db_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = "Hello {name}! Age {age}."
    db_session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(PromptValidationError) as excinfo:
        await StrictPromptLoader.load_and_validate(
            db_session, "test_prompt", ["name"]
        )
    assert "包含运行时无法识别的非法占位符" in str(excinfo.value)

@pytest.mark.asyncio
async def test_load_and_validate_db_error():
    db_session = MagicMock(spec=AsyncSession)
    db_session.execute = AsyncMock(side_effect=Exception("Connection refused"))

    with pytest.raises(PromptLoadError) as excinfo:
        await StrictPromptLoader.load_and_validate(
            db_session, "test_prompt", ["name"]
        )
    assert "数据库查询异常" in str(excinfo.value)

@pytest.mark.asyncio
async def test_load_and_validate_not_found():
    db_session = MagicMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db_session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(PromptLoadError) as excinfo:
        await StrictPromptLoader.load_and_validate(
            db_session, "test_prompt", ["name"]
        )
    assert "未找到处于激活状态且名称为" in str(excinfo.value)
