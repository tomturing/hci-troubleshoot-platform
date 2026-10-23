"""工单号用户指定与日志收集 resolve 的单元测试。

背景（2026-10-23）：工单号历史上只由 ``generate_case_id()`` 生成。日志收集入口需要
"用户给一个工单号：已存在则复用、不存在则按该号建档"，因此 ``CreateCase`` 支持显式
``case_id``。本组测试锁定：

1. 指定号优先于自动生成；
2. 不指定仍走自动生成（行为不变）；
3. resolve 已存在工单时**不**创建、不泄露内容；
4. resolve 不存在时用该号建档；
5. 并发下主键冲突视为"已存在"，事务回滚。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.models.case import Case, CaseStatus
from app.services.case_service import CaseService
from shared.models.schemas import CaseCreate
from sqlalchemy.exc import IntegrityError


def make_case(case_id: str, client_id: str = "client-a") -> Case:
    now = datetime.now(UTC)
    return Case(
        case_id=case_id,
        user_id=uuid4(),
        client_id=client_id,
        title="t",
        description=None,
        status=CaseStatus.created,
        assistant_type="htp-agent",
        priority="medium",
        category=None,
        metadata={},
        created_at=now,
        updated_at=now,
        confirmed_at=None,
        resolved_at=None,
        closed_at=None,
        close_reason=None,
        trace_id=None,
    )


@pytest.fixture
def service():
    repo = AsyncMock()
    repo.session = AsyncMock()
    repo.get_by_id = AsyncMock(return_value=None)
    repo.get_user_by_client_id = AsyncMock(return_value=None)
    repo.create_user = AsyncMock(return_value=SimpleNamespace(user_id=uuid4()))

    async def fake_create(case):
        # 模拟 DB 回填 server_default 时间戳（真实 flush 后由数据库生成）
        now = datetime.now(UTC)
        if getattr(case, "created_at", None) is None:
            case.created_at = now
        if getattr(case, "updated_at", None) is None:
            case.updated_at = now
        return case

    repo.create = AsyncMock(side_effect=fake_create)
    repo.generate_case_id = AsyncMock(return_value="Q2026102300001")
    return CaseService(repo), repo


def payload(**overrides) -> CaseCreate:
    data = {
        "client_id": "client-a",
        "title": "日志收集任务 Q2026101401684",
        "description": "test",
        "assistant_type": "htp-agent",
    }
    data.update(overrides)
    return CaseCreate(**data)


async def test_manual_case_id_takes_precedence(service):
    """指定号时必须直接使用，不得再调自动生成。"""
    svc, repo = service

    await svc.create_case(payload(case_id="Q2026101401684"))

    created = repo.create.await_args.args[0]
    assert created.case_id == "Q2026101401684"
    repo.generate_case_id.assert_not_called()


async def test_auto_generated_when_case_id_omitted(service):
    """不指定时行为不变：仍用 generate_case_id()。"""
    svc, repo = service

    await svc.create_case(payload())

    created = repo.create.await_args.args[0]
    assert created.case_id == "Q2026102300001"
    repo.generate_case_id.assert_awaited_once()


async def test_resolve_existing_case_reuses_without_leaking(service):
    """已存在工单直接复用：返回 created=False，且不创建、不返回工单内容。"""
    svc, repo = service
    repo.get_by_id = AsyncMock(return_value=make_case("Q2026101401684", client_id="client-b"))

    case_id, created = await svc.resolve_case_for_log_collect("Q2026101401684", "client-a")

    assert (case_id, created) == ("Q2026101401684", False)
    repo.create.assert_not_called()


async def test_resolve_creates_case_when_missing(service):
    """不存在时按用户指定的号建档，created=True。"""
    svc, repo = service

    case_id, created = await svc.resolve_case_for_log_collect("Q2026101401684", "client-a")

    assert (case_id, created) == ("Q2026101401684", True)
    created_case = repo.create.await_args.args[0]
    assert created_case.case_id == "Q2026101401684"
    assert created_case.client_id == "client-a"
    assert created_case.metadata == {"source": "log_collect"}


async def test_resolve_race_treated_as_existing(service):
    """并发下主键冲突：回滚并视为已存在，不向调用方抛错。"""
    svc, repo = service
    repo.create = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("dup")))

    case_id, created = await svc.resolve_case_for_log_collect("Q2026101401684", "client-a")

    assert (case_id, created) == ("Q2026101401684", False)
    repo.session.rollback.assert_awaited_once()
