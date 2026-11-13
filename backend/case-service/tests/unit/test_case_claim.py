"""壳工单数据访问判定与认领的行为测试。

背景（2026-11-13 Q2026111301229）：日志收集入口用外部工单号建壳工单（零会话零消息），
但 ``case.client_id`` 绑的是浏览器会话身份。同一个人换浏览器/清 Cookie 后：
  - 采集能跑（不校验归属），
  - 桥日志回采与终端录像回放必然 403（只校验归属），
  - 前端只有一句警告，没有任何出路，用户只能重新采集并覆盖节点上的包。

本组测试锁定修复后的边界：
1. 归属不动，服务端只增加"壳工单 + 本人任务"这一条窄口径；
2. 真实业务工单（有会话）绝不因"输入过单号并采集过"而放行；
3. 认领必须显式、幂等、带审计，且交付在途/原身份活跃时必须拒绝；
4. 认领把工单与名下采集任务一起转移，否则旧包/旧录像仍不可用。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import app.services.case_service as case_service_module
from app.models.case import Case, CaseStatus
from app.services.case_service import CaseService
from shared.security.case_access import resolve_data_access


class FakeSession:
    """只满足 ``session.execute(...).mappings().first()`` 的最小会话替身。"""

    def __init__(self, row, rowcount: int = 0):
        self._row = row
        self._rowcount = rowcount
        self.committed = False

    async def execute(self, *_args, **_kwargs):
        row, rowcount = self._row, self._rowcount

        class _Result:
            def mappings(self):
                return SimpleNamespace(first=lambda: row)

            @property
            def rowcount(self):
                return rowcount

        return _Result()

    async def commit(self):
        self.committed = True


def make_case(case_id: str, client_id: str, metadata: dict | None = None) -> Case:
    now = datetime.now(UTC)
    return Case(
        case_id=case_id,
        user_id=uuid4(),
        client_id=client_id,
        title=f"日志收集任务 {case_id}",
        description=None,
        status=CaseStatus.created,
        assistant_type="htp-agent",
        priority="medium",
        category=None,
        metadata_=metadata if metadata is not None else {"source": "log_collect"},
        created_at=now,
        updated_at=now,
        confirmed_at=None,
        resolved_at=None,
        closed_at=None,
        close_reason=None,
        trace_id=None,
    )


def access_row(client_id: str, *, source: str = "log_collect", conversations=0, messages=0, own_task=True):
    return {
        "client_id": client_id,
        "source": source,
        "conversations": conversations,
        "messages": messages,
        "own_task": own_task,
    }


@pytest.fixture
def service():
    repo = AsyncMock()
    repo.session = FakeSession(access_row("client-other"))
    repo.get_by_id = AsyncMock(return_value=make_case("Q1", "client-other"))
    repo.get_user_by_client_id = AsyncMock(return_value=None)
    repo.create_user = AsyncMock(return_value=SimpleNamespace(user_id=uuid4()))
    return CaseService(repo)


class TestDataAccess:
    async def test_owner_keeps_full_access(self):
        session = FakeSession(access_row("me"))
        access = await resolve_data_access(session, "Q1", "me")

        assert access["owned"] is True
        assert access["data_access"] is True

    async def test_shell_case_with_own_task_is_granted(self):
        """换客户端后仍能回采/回放：壳工单 + 本人采集任务。"""
        session = FakeSession(access_row("client-other", own_task=True))
        access = await resolve_data_access(session, "Q1", "me")

        assert access["owned"] is False
        assert access["shell_case"] is True
        assert access["task_granted"] is True
        assert access["data_access"] is True

    async def test_business_case_never_grants_by_task(self):
        """有会话的工单不能靠"采集过"扩权，否则等于猜中单号 + 点一次采集即可读。"""
        session = FakeSession(access_row("client-other", conversations=1, messages=8))
        access = await resolve_data_access(session, "Q1", "me")

        assert access["shell_case"] is False
        assert access["task_granted"] is False
        assert access["data_access"] is False

    async def test_source_marker_alone_is_not_enough(self):
        """来源标记被伪造/残留时，仍要靠零会话兜底。"""
        session = FakeSession(access_row("client-other", messages=2))
        access = await resolve_data_access(session, "Q1", "me")

        assert access["data_access"] is False


class TestClaim:
    async def test_foreign_case_is_not_claimable(self, service, monkeypatch):
        monkeypatch.setattr(
            case_service_module,
            "resolve_data_access",
            AsyncMock(return_value={"exists": True, "owned": False, "shell_case": False,
                                    "task_granted": False, "data_access": False}),
        )

        result = await service.claim_shell_case("Q1", "me")

        assert result["claimed"] is False
        assert result["blocked_reason"] == "not_shell_case"

    async def test_claim_transfers_case_and_tasks_with_audit(self, service, monkeypatch):
        monkeypatch.setattr(
            case_service_module,
            "resolve_data_access",
            AsyncMock(return_value={"exists": True, "owned": False, "shell_case": True,
                                    "task_granted": False, "data_access": False}),
        )
        monkeypatch.setattr(service, "_case_task_activity", AsyncMock(return_value=[]))
        service.repository.session = FakeSession(access_row("client-other"), rowcount=2)

        result = await service.claim_shell_case("Q1", "me")

        assert result["claimed"] is True
        assert result["tasks_moved"] == 2
        case = service.repository.get_by_id.return_value
        assert case.client_id == "me"
        # 审计必须留在案例元数据里（不新建表，Atlas 声明式 schema 会销毁额外表）
        claim = case.metadata_["claims"][-1]
        assert claim["from"] == "client-other"
        assert claim["to"] == "me"
        # 单测无 otel 上下文时 trace 为空串，但字段必须在（生产由调用链填充）
        assert "trace_id" in claim
        assert service.repository.session.committed is True

    @pytest.mark.parametrize(
        ("delivery_status", "expected"),
        [("pulling", "delivery_in_progress"), ("uploading", "delivery_in_progress"),
         ("relaying", "delivery_in_progress")],
    )
    async def test_claim_rejected_while_delivery_in_flight(self, service, monkeypatch, delivery_status, expected):
        monkeypatch.setattr(
            case_service_module,
            "resolve_data_access",
            AsyncMock(return_value={"exists": True, "owned": False, "shell_case": True,
                                    "task_granted": False, "data_access": False}),
        )
        monkeypatch.setattr(
            service,
            "_case_task_activity",
            AsyncMock(return_value=[{"created_by": "client-other", "status": "uploading",
                                     "delivery_status": delivery_status,
                                     "updated_at": datetime.now(UTC)}]),
        )

        result = await service.claim_shell_case("Q1", "me")

        assert result["claimed"] is False
        assert result["blocked_reason"] == expected
        assert service.repository.session.committed is False

    async def test_claim_rejected_while_owner_still_active(self, service, monkeypatch):
        monkeypatch.setattr(
            case_service_module,
            "resolve_data_access",
            AsyncMock(return_value={"exists": True, "owned": False, "shell_case": True,
                                    "task_granted": False, "data_access": False}),
        )
        monkeypatch.setattr(
            service,
            "_case_task_activity",
            AsyncMock(return_value=[{"created_by": "client-other", "status": "completed",
                                     "delivery_status": "queued",
                                     "updated_at": datetime.now(UTC) - timedelta(seconds=5)}]),
        )

        result = await service.claim_shell_case("Q1", "me")

        assert result["claimed"] is False
        assert result["blocked_reason"] == "owner_active"

    async def test_claim_after_idle_window_succeeds(self, service, monkeypatch):
        """原身份空闲超过窗口 → 可认领（本案 Q2026111301229：07:48 采完，17:13 仍在等）。"""
        monkeypatch.setattr(
            case_service_module,
            "resolve_data_access",
            AsyncMock(return_value={"exists": True, "owned": False, "shell_case": True,
                                    "task_granted": False, "data_access": False}),
        )
        monkeypatch.setattr(
            service,
            "_case_task_activity",
            AsyncMock(return_value=[{"created_by": "client-other", "status": "completed",
                                     "delivery_status": "queued",
                                     "updated_at": datetime.now(UTC) - timedelta(hours=4)}]),
        )

        result = await service.claim_shell_case("Q1", "me")

        assert result["claimed"] is True
        assert result["data_access"] is True

    async def test_already_owned_claim_is_idempotent(self, service, monkeypatch):
        monkeypatch.setattr(
            case_service_module,
            "resolve_data_access",
            AsyncMock(return_value={"exists": True, "owned": True, "shell_case": True,
                                    "task_granted": False, "data_access": True}),
        )

        result = await service.claim_shell_case("Q1", "me")

        assert result["claimed"] is False
        assert result["already_owned"] is True