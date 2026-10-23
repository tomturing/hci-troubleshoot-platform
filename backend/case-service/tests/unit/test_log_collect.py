"""日志收集任务：状态机、业务规则与参数校验测试。"""

import re
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.models.log_collect import LogCollectTask
from app.repositories.log_collect_repo import LogCollectTaskRepository
from app.schemas.log_collect import LogCollectTaskCreate, LogCollectTaskPatch
from app.schemas.log_collect_errors import build_problem, load_error_catalog
from app.services.log_collect_service import (
    InvalidTransitionError,
    LogCollectService,
    TaskConflictError,
    TaskNotFoundError,
)
from pydantic import ValidationError


def make_payload(**overrides) -> LogCollectTaskCreate:
    data = {
        "case_id": "Q20261022001",
        # 工单号统一为平台工单号：ticket_no 必须等于 case_id
        "ticket_no": "Q20261022001",
        "origin_node_ip": "172.28.25.1",
        "modules": ["vm_suspend"],
        "date_from": date(2026, 10, 21),
        "date_to": date(2026, 10, 22),
        "ips": ["all"],
    }
    data.update(overrides)
    return LogCollectTaskCreate(**data)


@pytest.fixture
def mock_repo() -> MagicMock:
    return MagicMock(spec=LogCollectTaskRepository)


@pytest.fixture
def service(mock_repo) -> LogCollectService:
    return LogCollectService(mock_repo)


class TestStatusMachine:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("pending", "probing"),
            ("probing", "installing"),
            ("installing", "collecting"),
            ("collecting", "packaging"),
            ("packaging", "uploading"),
            ("uploading", "completed"),
            ("collecting", "completed"),  # 无上传场景允许跳步
            ("collecting", "failed"),
            ("pending", "cancelled"),
        ],
    )
    def test_allowed(self, current, target):
        LogCollectService.validate_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("completed", "collecting"),  # 终态不可复活
            ("failed", "pending"),
            ("cancelled", "collecting"),
            ("pending", "uploading"),  # 不能跳过采集
            ("uploading", "collecting"),  # 不能倒退阶段
        ],
    )
    def test_rejected(self, current, target):
        with pytest.raises(InvalidTransitionError):
            LogCollectService.validate_transition(current, target)

    def test_same_status_is_idempotent(self):
        LogCollectService.validate_transition("collecting", "collecting")


class TestCreateTask:
    async def test_create_success(self, service, mock_repo):
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        created = MagicMock(spec=LogCollectTask)
        mock_repo.create = AsyncMock(return_value=created)

        result = await service.create_task(make_payload(), "client-1")

        assert result is created
        task = mock_repo.create.await_args.args[0]
        assert task.ticket_no == "Q20261022001"
        assert task.status == "pending"
        assert task.progress == 0
        assert task.created_by == "client-1"
        assert task.ips == ["all"]

    async def test_conflict_when_active_task_exists(self, service, mock_repo):
        active = MagicMock(spec=LogCollectTask)
        active.task_id = "11111111-1111-1111-1111-111111111111"
        mock_repo.get_active_by_case = AsyncMock(return_value=active)

        with pytest.raises(TaskConflictError):
            await service.create_task(make_payload(), "client-1")
        mock_repo.create.assert_not_called()

    async def test_trace_id_generated_when_absent(self, service, mock_repo):
        """前端未传 trace_id 时必须由平台兜底生成，保证全链路可串联。"""
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        await service.create_task(make_payload(), "client-1")

        task = mock_repo.create.await_args.args[0]
        assert isinstance(task.trace_id, str)
        assert len(task.trace_id) == 32  # W3C trace_id：32 位小写 hex

    async def test_trace_id_preserved_when_provided(self, service, mock_repo):
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        await service.create_task(make_payload(trace_id="a" * 32), "client-1")

        task = mock_repo.create.await_args.args[0]
        assert task.trace_id == "a" * 32


class TestUpdateTask:
    async def test_progress_regression_rejected(self, service, mock_repo):
        task = MagicMock(spec=LogCollectTask)
        task.status = "collecting"
        task.progress = 60
        mock_repo.get_by_id = AsyncMock(return_value=task)

        with pytest.raises(InvalidTransitionError):
            await service.update_task("11111111-1111-1111-1111-111111111111", LogCollectTaskPatch(progress=50))

    async def test_completed_forces_progress_100(self, service, mock_repo):
        task = MagicMock(spec=LogCollectTask)
        task.status = "uploading"
        task.progress = 90
        mock_repo.get_by_id = AsyncMock(return_value=task)
        mock_repo.flush = AsyncMock()
        mock_repo.refresh = AsyncMock()

        result = await service.update_task(
            "11111111-1111-1111-1111-111111111111", LogCollectTaskPatch(status="completed")
        )

        assert result.progress == 100
        assert result.finished_at is not None

    async def test_terminal_task_cannot_revive(self, service, mock_repo):
        task = MagicMock(spec=LogCollectTask)
        task.status = "completed"
        task.progress = 100
        mock_repo.get_by_id = AsyncMock(return_value=task)

        with pytest.raises(InvalidTransitionError):
            await service.update_task("11111111-1111-1111-1111-111111111111", LogCollectTaskPatch(status="collecting"))


class TestCancelTask:
    async def test_cancel_active_task(self, service, mock_repo):
        task = MagicMock(spec=LogCollectTask)
        task.status = "collecting"
        mock_repo.get_by_id = AsyncMock(return_value=task)
        mock_repo.flush = AsyncMock()
        mock_repo.refresh = AsyncMock()

        result = await service.cancel_task("11111111-1111-1111-1111-111111111111", "测试取消")

        assert result.status == "cancelled"
        assert result.error_code == "cancelled_by_user"
        assert result.error_message == "测试取消"

    async def test_cancel_terminal_task_rejected(self, service, mock_repo):
        task = MagicMock(spec=LogCollectTask)
        task.status = "completed"
        mock_repo.get_by_id = AsyncMock(return_value=task)

        with pytest.raises(TaskConflictError):
            await service.cancel_task("11111111-1111-1111-1111-111111111111")


class TestErrorCatalog:
    """错误码目录与 Problem Details 契约。"""

    def test_code_format_and_layer_consistency(self):
        catalog = load_error_catalog()
        pattern = re.compile(r"^LC-(ENV|INSTALL|EXEC|PLUGIN|TRANSFER|PLATFORM)-\d{3}$")
        for code, meta in catalog.items():
            assert pattern.match(code), code
            # 码前缀必须与 layer 同构，否则"看码定界"失效
            assert code.split("-")[1] == meta["layer"], code

    def test_problem_structure(self):
        problem = build_problem("LC-PLUGIN-007", "该工单已有进行中的任务", trace_id="a" * 32)
        assert problem["type"] == "urn:hci:log-collect:error:LC-PLUGIN-007"
        assert problem["status"] == 409
        assert problem["layer"] == "PLUGIN"
        assert problem["owner"] == "platform"
        assert problem["retryable"] == "manual"
        assert problem["trace_id"] == "a" * 32

    def test_unknown_code_falls_back(self):
        problem = build_problem("LC-NOPE-999", "unknown")
        assert problem["status"] == 500
        assert problem["code"] == "LC-NOPE-999"

    def test_exception_carries_code(self):
        assert TaskNotFoundError("x").code == "LC-PLUGIN-008"
        assert TaskConflictError("x").code == "LC-PLUGIN-007"
        assert InvalidTransitionError("x").code == "LC-PLATFORM-003"
        assert InvalidTransitionError("x", code="LC-PLATFORM-004").code == "LC-PLATFORM-004"


class TestPayloadValidation:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"ticket_no": "Q1;rm -rf /"},
            {"ticket_no": "Q1 2"},
            {"ticket_no": "A" * 33},
            {"origin_node_ip": "not-an-ip"},
            {"ips": ["1.2.3.999"]},
            {"ips": ["1.1.1.1;id"]},
            {"extra_paths": ["relative/path"]},
            {"extra_paths": ["/sf/../etc/shadow"]},
            {"extra_paths": ["/sf/x;id"]},
            {"remote_url": "ftp://server/path"},
            {"date_from": date(2026, 8, 1)},
            {"date_to": date(2026, 10, 20)},
        ],
    )
    def test_invalid_payloads(self, overrides):
        with pytest.raises(ValidationError):
            make_payload(**overrides)

    def test_ticket_no_derived_from_case_id_when_omitted(self):
        """工单号只有一个：未传 ticket_no 时自动取 case_id。"""
        payload = LogCollectTaskCreate(
            case_id="Q20261022001",
            origin_node_ip="172.28.25.1",
            modules=["vm_suspend"],
            date_from=date(2026, 10, 21),
            date_to=date(2026, 10, 22),
        )
        assert payload.ticket_no == "Q20261022001"

    def test_ticket_no_must_equal_case_id(self):
        """禁止两套工单号：显式传入不一致的 ticket_no 直接拒绝。"""
        with pytest.raises(ValidationError, match="ticket_no 必须与 case_id 一致"):
            make_payload(ticket_no="Q20261022002")

    def test_modules_deduplicated(self):
        payload = make_payload(modules=["network", "network", "vm_start"])
        assert payload.modules == ["network", "vm_start"]

    def test_empty_ips_default_to_all(self):
        payload = make_payload(ips=[])
        assert payload.ips == ["all"]

    def test_patch_requires_field(self):
        with pytest.raises(ValidationError):
            LogCollectTaskPatch()
