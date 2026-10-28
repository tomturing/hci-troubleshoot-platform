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

    async def test_probe_facts_persisted(self, service, mock_repo):
        """探测事实（arch / acli_version）必须随任务落库。

        失败任务事后要能自证运行环境，否则无法区分「acli 版本不支持 log_collect」
        与「参数/环境错误」；2026-10-27 实测 38/38 任务的这两个字段全为 NULL。
        """
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        await service.create_task(make_payload(arch="x86_64", acli_version="1.0.1"), "client-1")

        task = mock_repo.create.await_args.args[0]
        assert task.arch == "x86_64"
        assert task.acli_version == "1.0.1"

    async def test_probe_snapshot_persisted_with_task(self, service, mock_repo):
        """探测快照必须随任务落库。

        2026-10-28 现场：Q2026102800331 的 detail 只有 baseline_report_hash，
        事后无法回答"节点清单为什么是空的、日志服务器通不通、哪版 acli"。
        """
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        probe = {
            "acli_version": "1.0.0",
            "acli_build_time": "2026-05-20 17:22:16",
            "architecture": "x86_64",
            "hosts_count": 0,
            "catalog_source": "acli_help",
            "catalog_note": "节点清单获取失败：open /sys/fs/cgroup/cpu/cpuset.cpus",
            "note": "curl 探测失败：000 10.006299",
            "log_server_reachable": False,
            "http_code": 0,
        }
        await service.create_task(
            make_payload(
                upload_mode="bridge_relay",
                probe=probe,
                baseline_report_hash="226-9204f7de",
            ),
            "client-1",
        )

        task = mock_repo.create.await_args.args[0]
        assert task.detail["baseline_report_hash"] == "226-9204f7de"
        assert task.detail["probe"]["hosts_count"] == 0
        assert task.detail["probe"]["log_server_reachable"] is False
        assert "cpuset.cpus" in task.detail["probe"]["catalog_note"]

    async def test_probe_snapshot_oversize_keeps_minimal(self, service, mock_repo):
        """探测快照超限时只保留可对账的最小集合，不让 detail 无界增长。"""
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        await service.create_task(
            make_payload(probe={"extra": "x" * 9000, "acli_version": "1.0.0"}),
            "client-1",
        )

        task = mock_repo.create.await_args.args[0]
        assert task.detail["probe"]["truncated"] is True
        assert task.detail["probe"]["acli_version"] == "1.0.0"

    async def test_upload_mode_persisted_on_create(self, service, mock_repo):
        """交付路径必须在创建那一刻落库。

        只在交付完成时补写的话：采集中的任务读不到它、P1 失败降级后也无法区分
        "用户本来就选 P2"与"P1 失败被迫改 P2"。
        """
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        await service.create_task(
            make_payload(upload_mode="acli_direct", remote_url="http://acli.sangfor.com.cn:5888"),
            "client-1",
        )

        task = mock_repo.create.await_args.args[0]
        assert task.upload_mode == "acli_direct"
        assert task.remote_url == "http://acli.sangfor.com.cn:5888"

    async def test_upload_mode_inferred_from_remote_url(self, service, mock_repo):
        """未传 upload_mode 时按 remote_url 推断，兼容未升级的客户端。"""
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        await service.create_task(make_payload(remote_url="http://acli.sangfor.com.cn:5888"), "client-1")
        assert mock_repo.create.await_args.args[0].upload_mode == "acli_direct"

        await service.create_task(make_payload(), "client-1")
        assert mock_repo.create.await_args.args[0].upload_mode == "bridge_relay"

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

    async def test_baseline_report_hash_persisted_into_detail(self, service, mock_repo):
        """下发锚定的报告指纹必须随任务落库。

        指纹只存在前端内存里时，关面板/换浏览器/重启后接管任务就无法判断
        「节点报告属于哪一次下发」，只能靠猜（2026-10-26 现场两次误判的根源）。
        """
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        await service.create_task(make_payload(baseline_report_hash="7843-1a2b3c4d"), "client-1")

        task = mock_repo.create.await_args.args[0]
        assert task.detail == {"baseline_report_hash": "7843-1a2b3c4d"}

    async def test_detail_empty_when_baseline_absent(self, service, mock_repo):
        """没有基线时 detail 保持空对象：不写入假指纹，前端据此退化为"按节点态为准"。"""
        mock_repo.get_active_by_case = AsyncMock(return_value=None)
        mock_repo.create = AsyncMock(return_value=MagicMock(spec=LogCollectTask))

        await service.create_task(make_payload(), "client-1")

        task = mock_repo.create.await_args.args[0]
        assert task.detail == {}
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

    async def test_detail_patch_merges_instead_of_overwrite(self, service, mock_repo):
        """detail 是审计容器，PATCH 必须合并。

        整块覆盖的后果很具体：P1 失败降级时写一条"降级原因"，
        就会把下发时锚定的 baseline_report_hash 冲掉，之后所有归属判定失效。
        """
        task = MagicMock(spec=LogCollectTask)
        task.status = "collecting"
        task.progress = 10
        task.detail = {"baseline_report_hash": "7843-1a2b3c4d"}
        mock_repo.get_by_id = AsyncMock(return_value=task)
        mock_repo.flush = AsyncMock()
        mock_repo.refresh = AsyncMock()

        result = await service.update_task(
            "11111111-1111-1111-1111-111111111111",
            LogCollectTaskPatch(detail={"upload_mode_switched_from": "acli_direct"}),
        )

        assert result.detail == {
            "baseline_report_hash": "7843-1a2b3c4d",
            "upload_mode_switched_from": "acli_direct",
        }

    async def test_detail_patch_null_removes_key(self, service, mock_repo):
        """显式传 null 表示删除该键：撤销降级记录时不能留下悬空字段。"""
        task = MagicMock(spec=LogCollectTask)
        task.status = "collecting"
        task.progress = 10
        task.detail = {"baseline_report_hash": "7843-1a2b3c4d", "upload_mode_switched_from": "acli_direct"}
        mock_repo.get_by_id = AsyncMock(return_value=task)
        mock_repo.flush = AsyncMock()
        mock_repo.refresh = AsyncMock()

        result = await service.update_task(
            "11111111-1111-1111-1111-111111111111",
            LogCollectTaskPatch(detail={"upload_mode_switched_from": None}),
        )

        assert result.detail == {"baseline_report_hash": "7843-1a2b3c4d"}


class TestBeginDelivery:
    """补交付的登记语义：只推进 delivery_*，并把实际交付路径改成 P2。"""

    async def test_registers_bridge_relay_and_keeps_collection_status(self, service, mock_repo):
        task = MagicMock(spec=LogCollectTask)
        task.status = "completed"
        task.upload_mode = "acli_direct"
        task.delivery_status = "none"
        task.delivery_attempts = 0
        mock_repo.get_by_id = AsyncMock(return_value=task)
        mock_repo.flush = AsyncMock()
        mock_repo.refresh = AsyncMock()

        result = await service.begin_delivery("11111111-1111-1111-1111-111111111111")

        # acli 没有独立重传接口：包一旦没走成 P1，补交付只能是 P2
        assert result.upload_mode == "bridge_relay"
        assert result.delivery_status == "queued"
        assert result.delivery_attempts == 1
        assert result.delivery_started_at is not None
        # 采集结论不能被交付动作改写
        assert result.status == "completed"


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

    # 交付路径与上传地址是同一决策的两个投影：P1 的唯一实现是 acli 带 -u 直传，
    # 因此"有路径无地址"无法执行；"给地址却声明 P2"会让"平台留副本"的承诺失效。
    # 矛盾时拒绝，而不是静默取一个——静默取舍正是路径不可追溯的成因。
    def test_upload_mode_p1_requires_remote_url(self):
        with pytest.raises(ValidationError, match="P1 直传必须提供"):
            make_payload(upload_mode="acli_direct")

    def test_upload_mode_p2_rejects_remote_url(self):
        with pytest.raises(ValidationError, match="不应提供 remote_url"):
            make_payload(upload_mode="bridge_relay", remote_url="http://acli.sangfor.com.cn:5888")

    def test_upload_mode_rejects_unknown_value(self):
        with pytest.raises(ValidationError):
            make_payload(upload_mode="platform_direct")

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

    # all 与具体 IP 是互斥语义：静默收窄会让落库结果与节点实际收到的命令不一致。
    def test_ips_rejects_all_mixed_with_ip(self):
        with pytest.raises(ValidationError, match="不能与具体 IP 混写"):
            make_payload(ips=["all", "172.28.25.2"])

    def test_ips_blank_entries_do_not_count_as_hosts(self):
        payload = make_payload(ips=["all", "  "])
        assert payload.ips == ["all"]

    def test_patch_requires_field(self):
        with pytest.raises(ValidationError):
            LogCollectTaskPatch()
