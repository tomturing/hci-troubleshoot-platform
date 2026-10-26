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
        "case_id": "Q2026102200001",
        # 工单号统一为平台工单号：ticket_no 必须等于 case_id
        "ticket_no": "Q2026102200001",
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
            # 接管：节点仍在跑时，平台的失败/取消投影允许被覆盖回采集中
            ("failed", "collecting"),
            ("cancelled", "collecting"),
            # 纠正：完成判定不可信（历史任务报告）时，允许用户显式改回采集中
            ("completed", "collecting"),
        ],
    )
    def test_allowed(self, current, target):
        LogCollectService.validate_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("completed", "uploading"),  # 归档后不能退到上传阶段，只能整条纠正
            ("completed", "failed"),
            ("failed", "pending"),
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
        assert task.case_id == "Q2026102200001"
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

    async def test_completed_correction_clears_artifact_projection(self, service, mock_repo):
        """completed → collecting 只用于"以节点为准纠正"，必须清空上一轮的产物投影。

        原产物可能属于同工单的上一次任务（重复 create 会拿到旧报告），
        继续挂着会变成"采集中却显示产物位置"的新误导；只改 DB 投影，压缩包一律不删。
        """
        task = MagicMock(spec=LogCollectTask)
        task.status = "completed"
        task.progress = 100
        task.archive_path = "/sf/data/local/acli/tasks/Q1.tar.gz"
        task.archive_size = 1024
        task.archive_sha256 = "a" * 64
        task.relay_status = "success"
        task.relay_chunk_index = 3
        task.relay_chunk_total = 3
        task.relay_restarts = 2
        task.cleanup_state = "done"
        task.cleanup_reason = "already"
        mock_repo.get_by_id = AsyncMock(return_value=task)
        mock_repo.flush = AsyncMock()
        mock_repo.refresh = AsyncMock()

        result = await service.update_task(
            "11111111-1111-1111-1111-111111111111", LogCollectTaskPatch(status="collecting")
        )

        assert result.archive_path is None
        assert result.archive_size is None
        assert result.archive_sha256 is None
        assert result.relay_status == "none"
        assert result.relay_restarts == 0
        assert result.cleanup_state == "none"
        assert result.finished_at is None
        # 原产物值必须留在审计文案里，方便事后核对"当时到底归档了什么"
        assert result.error_code == "corrected_by_node"
        assert "/sf/data/local/acli/tasks/Q1.tar.gz" in result.error_message
        assert "success" in result.error_message

    async def test_adopt_clears_failure_traces(self, service, mock_repo):
        """failed → collecting（接管）必须清掉上次的失败痕迹。

        否则界面会一边显示"采集中"一边显示旧错误原因。
        """
        task = MagicMock(spec=LogCollectTask)
        task.status = "failed"
        task.progress = 30
        task.error_code = "LC-PLUGIN-009"
        task.error_message = "上次采集失败"
        mock_repo.get_by_id = AsyncMock(return_value=task)
        mock_repo.flush = AsyncMock()
        mock_repo.refresh = AsyncMock()

        result = await service.update_task(
            "11111111-1111-1111-1111-111111111111", LogCollectTaskPatch(status="collecting")
        )

        assert result.error_code is None
        assert result.error_message is None
        assert result.finished_at is None


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
            {"case_id": "Q1;rm -rf /"},
            {"case_id": "Q20261022 0001"},
            {"case_id": "Q20261022000011"},
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

    def test_case_id_accepts_manual_number(self):
        """用户指定的工单号（Q + 8 位日期 + 5 位序号）可直接用于日志任务。"""
        payload = make_payload(case_id="Q2026101401684")
        assert payload.case_id == "Q2026101401684"

    def test_case_id_rejects_invalid_calendar_date(self):
        """格式合法但日期不存在（2026-13-32）必须拒绝。"""
        with pytest.raises(ValidationError, match="日期不合法"):
            make_payload(case_id="Q2026133200001")

    def test_modules_deduplicated(self):
        payload = make_payload(modules=["network", "network", "vm_start"])
        assert payload.modules == ["network", "vm_start"]

    def test_empty_ips_default_to_all(self):
        payload = make_payload(ips=[])
        assert payload.ips == ["all"]

    def test_patch_requires_field(self):
        with pytest.raises(ValidationError):
            LogCollectTaskPatch()
