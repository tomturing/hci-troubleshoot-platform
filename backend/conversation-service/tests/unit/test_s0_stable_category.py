"""S0 稳定分类身份、权威校验和原子推进回归测试。"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _service(*, kb_client=None, session_factory=None):
    from app.services.conversation_service import ConversationService

    repository = MagicMock()
    repository.session = MagicMock()
    return ConversationService(
        repository=repository,
        ai_registry=MagicMock(),
        kb_client=kb_client,
        session_factory=session_factory,
    )


@pytest.mark.asyncio
async def test_validate_s0_category_returns_authoritative_name():
    kb_client = MagicMock()
    kb_client.get_categories_grouped = AsyncMock(
        return_value={"虚拟机": [{"code": "虚拟机-003", "name": "虚拟机开机失败"}]}
    )
    service = _service(kb_client=kb_client)

    result = await service._validate_s0_category(
        {"code": "虚拟机-003", "name": "模型不可信名称"}
    )

    assert result == {"code": "虚拟机-003", "name": "虚拟机开机失败"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grouped,category",
    [
        ({}, {"code": "虚拟机-003", "name": "虚拟机开机失败"}),
        (
            {"虚拟机": [{"code": "虚拟机-003", "name": "虚拟机开机失败"}]},
            {"code": "存储-020", "name": "虚拟存储性能告警"},
        ),
    ],
)
async def test_validate_s0_category_fails_closed(grouped, category):
    kb_client = MagicMock()
    kb_client.get_categories_grouped = AsyncMock(return_value=grouped)

    assert await _service(kb_client=kb_client)._validate_s0_category(category) is None


@pytest.mark.asyncio
async def test_validate_s0_category_fails_closed_on_registry_error():
    kb_client = MagicMock()
    kb_client.get_categories_grouped = AsyncMock(side_effect=RuntimeError("KB unavailable"))

    assert (
        await _service(kb_client=kb_client)._validate_s0_category(
            {"code": "虚拟机-003", "name": "虚拟机开机失败"}
        )
        is None
    )


def _session_factory(*, update_rowcount: int):
    duplicate_result = MagicMock()
    duplicate_result.scalar_one_or_none.return_value = None
    update_result = MagicMock()
    update_result.rowcount = update_rowcount

    session = MagicMock()
    session.execute = AsyncMock(side_effect=[duplicate_result, update_result])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=context)
    return factory, session


@pytest.mark.asyncio
async def test_commit_s0_confirmation_commits_before_external_hit():
    factory, session = _session_factory(update_rowcount=1)
    kb_client = MagicMock()

    async def increment_after_commit(_code):
        session.commit.assert_awaited_once()
        return 8

    kb_client.increment_category_hit = AsyncMock(side_effect=increment_after_commit)
    service = _service(kb_client=kb_client, session_factory=factory)

    with patch("app.services.conversation_service.settings.CASE_SERVICE_URL", ""):
        result = await service._commit_s0_confirmation(
            conversation_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
            case_id="Q2026072855923",
            category_info={"code": "虚拟机-003", "name": "虚拟机开机失败"},
        )

    assert result is True
    assert session.execute.await_count == 2
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    kb_client.increment_category_hit.assert_awaited_once_with("虚拟机-003")


@pytest.mark.asyncio
async def test_commit_s0_confirmation_rejects_stage_conflict():
    factory, session = _session_factory(update_rowcount=0)
    kb_client = MagicMock()
    kb_client.increment_category_hit = AsyncMock()
    service = _service(kb_client=kb_client, session_factory=factory)

    result = await service._commit_s0_confirmation(
        conversation_id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
        case_id="Q2026072855923",
        category_info={"code": "虚拟机-003", "name": "虚拟机开机失败"},
    )

    assert result is False
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    kb_client.increment_category_hit.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_s0_confirmation_does_not_fail_after_statistics_error():
    factory, session = _session_factory(update_rowcount=1)
    kb_client = MagicMock()
    kb_client.increment_category_hit = AsyncMock(side_effect=RuntimeError("statistics unavailable"))
    service = _service(kb_client=kb_client, session_factory=factory)

    with patch("app.services.conversation_service.settings.CASE_SERVICE_URL", ""):
        result = await service._commit_s0_confirmation(
            conversation_id=uuid.UUID("00000000-0000-0000-0000-000000000005"),
            case_id="Q2026072855923",
            category_info={"code": "虚拟机-003", "name": "虚拟机开机失败"},
        )

    assert result is True
    session.commit.assert_awaited_once()
    kb_client.increment_category_hit.assert_awaited_once_with("虚拟机-003")
