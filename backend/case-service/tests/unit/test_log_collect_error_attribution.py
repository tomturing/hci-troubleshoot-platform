"""失败归因测试：error_code 必须在保存边界被确定性补全。

覆盖两类不变量：
1. 已知文案 → 规范码（且码必须在 catalog 中真实存在，禁止臆造）；
2. 未知/空文案 → 未归因兜底码，绝不返回空 —— 空码会让面板聚合与告警永久失明。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.schemas.log_collect_errors import load_error_catalog
from app.services.log_collect_error_attribution import (
    _RULES,
    BRIDGE_CHANNEL_LOST_CODE,
    UNATTRIBUTED_CODE,
    derive_error_code,
)
from app.services.log_collect_service import LogCollectService


class TestDeriveErrorCode:
    @pytest.mark.parametrize(
        "message,expected",
        [
            # 节点磁盘（ENV）与客户机磁盘（TRANSFER）必须分开：
            # 前者清 /sf/data/local，后者清 %TEMP%，处置动作完全不同。
            ("/sf/data/local 分区可用空间 3GB 小于 6GB", "LC-ENV-004"),
            ("客户机磁盘空间不足，需要 8GB", "LC-TRANSFER-008"),
            ("客户机还有其它交付任务在跑，并发容量不足", "LC-TRANSFER-009"),
            # 通道断开是独立语义：节点侧没有留下任务，重连后重试安全。
            ("与 terminal_bridge 的连接已断开，节点操作未送达", BRIDGE_CHANNEL_LOST_CODE),
            # SSH 会话
            ("SSH 会话不存在，请先 ssh_connect", "LC-ENV-006"),
            # acli 缺失/版本过旧
            ("bash: acli: command not found", "LC-EXEC-003"),
            ("未知的命令或者命名空间: log_collect", "LC-EXEC-003"),
            ("fork/exec /tmp/log_collect/plugin: no such file or directory", "LC-EXEC-003"),
            # 插件语义
            ("节点上已有进行中的任务，采集任务冲突", "LC-PLUGIN-007"),
            ("节点上已无任务，采集中断", "LC-PLUGIN-008"),
            ("采集未完成，进度停在 80%", "LC-PLUGIN-010"),
            # 制品：源包仍在写入必须先于泛化的分片失败命中
            ("源包已变化（上传会话大小不一致），需按新基线重建", "LC-TRANSFER-010"),
            ("节点压缩包在 5 分钟内仍未写完", "LC-TRANSFER-010"),
            ("本地副本不存在，无法补交付", "LC-TRANSFER-001"),
            ("校验和不一致：期望 abc 实际 def", "LC-TRANSFER-002"),
            ("上传中断：已传 3/10 片", "LC-TRANSFER-003"),
            # 平台存储
            ("平台日志存储目录不可写", "LC-PLATFORM-007"),
            ("平台日志存储空间不足，需要 2GB", "LC-PLATFORM-008"),
            # 平台 5xx：原因已知（我们自己 500），不能算「未归因」
            ("Request failed with status code 500", "LC-PLATFORM-012"),
            ("平台收尾失败 HTTP 500（平台暂时不可用，将退避重试）", "LC-PLATFORM-012"),
            ("[INTERNAL_ERROR] 服务内部错误（POST /api/log-collect/deliver）", "LC-PLATFORM-012"),
            # 僵尸交付会话：交付从未执行，处置动作明确（补交付）
            ("采集已终态（completed），交付会话仍排队中，判定为僵尸会话已清理（061）", "LC-PLATFORM-013"),
            # 超时
            ("命令执行超时（600s）", "LC-EXEC-002"),
        ],
    )
    def test_known_messages_map_to_catalog_code(self, message, expected):
        assert derive_error_code(message) == expected

    @pytest.mark.parametrize("message", [None, "", "   ", "某种没人见过的失败"])
    def test_unknown_or_empty_message_falls_back_instead_of_blank(self, message):
        """没有可识别原因时也必须给出码：留空等于放弃归因。"""
        code = derive_error_code(message)
        assert code == UNATTRIBUTED_CODE
        assert code  # 绝不返回空串

    def test_every_rule_code_exists_in_catalog(self):
        """规则只能引用目录里真实存在的码，防止拼写漂移。"""
        catalog = load_error_catalog()
        assert catalog, "错误码目录应可加载"
        unknown = sorted({code for _, code in _RULES if code not in catalog})
        assert unknown == [], f"规则引用了目录中不存在的码: {unknown}"

    def test_fallback_codes_exist_in_catalog(self):
        catalog = load_error_catalog()
        assert UNATTRIBUTED_CODE in catalog
        assert BRIDGE_CHANNEL_LOST_CODE in catalog

    def test_rule_order_is_specific_first(self):
        """客户机磁盘不足必须先于泛化的「磁盘空间不足」命中。

        顺序写反会把"清临时目录即可"误判成"清节点 /sf/data/local"，
        用户按错误提示操作会把力气花在错误的机器上。
        """
        needles = [needle for needle, _ in _RULES]
        assert needles.index("客户机磁盘空间不足") < needles.index("磁盘空间不足")
        assert needles.index("上传会话大小不一致") < needles.index("校验和不一致")


class TestAttributeFailure:
    """保存边界补码：只补空、不覆盖、非失败不写。"""

    @staticmethod
    def _task(**kwargs):
        base = {
            "task_id": "00000000-0000-0000-0000-000000000001",
            "status": "failed",
            "delivery_status": "none",
            "error_code": None,
            "error_message": None,
            "delivery_error": None,
            "detail": None,
        }
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_collect_failure_without_code_is_attributed(self):
        task = self._task(error_message="节点上已无任务，采集中断")
        LogCollectService.attribute_failure(task)
        assert task.error_code == "LC-PLUGIN-008"
        assert task.detail["error_code_derivation"]["stage"] == "collect"

    def test_delivery_failure_uses_delivery_error(self):
        task = self._task(
            status="completed",
            delivery_status="failed",
            delivery_error="客户机磁盘空间不足",
        )
        LogCollectService.attribute_failure(task)
        assert task.error_code == "LC-TRANSFER-008"
        assert task.detail["error_code_derivation"]["stage"] == "delivery"

    def test_existing_code_is_never_overwritten(self):
        task = self._task(error_code="LC-ENV-004", error_message="客户机磁盘空间不足")
        LogCollectService.attribute_failure(task)
        assert task.error_code == "LC-ENV-004"
        assert task.detail is None

    def test_non_failed_task_is_untouched(self):
        task = self._task(status="collecting", delivery_status="queued", error_message="进度 30%")
        LogCollectService.attribute_failure(task)
        assert task.error_code is None

    def test_empty_message_still_gets_code(self):
        task = self._task(status="failed", error_message="")
        LogCollectService.attribute_failure(task)
        assert task.error_code == UNATTRIBUTED_CODE

    def test_existing_detail_is_preserved(self):
        task = self._task(error_message="上传中断", detail={"baseline_report_hash": "abc"})
        LogCollectService.attribute_failure(task)
        assert task.detail["baseline_report_hash"] == "abc"
        assert task.detail["error_code_derivation"]["code"] == "LC-TRANSFER-003"


class TestBackfillMigrationConsistency:
    """062 回填迁移的规则表必须与 Python 侧逐条一致。

    SQL 无法 import Python，规则只能复制一份。若两边漂移，会出现
    「同一条失败文案，历史数据回填成一个码、新数据归因成另一个码」——
    面板按码聚合时会凭空多出两个桶，比空码更难排查。
    """

    _MIGRATION = (
        Path(__file__).resolve().parents[4] / "database" / "data-migrations" / "062_log_collect_error_code_backfill.sql"
    )
    _ROW = re.compile(r"^\s*\(\d+,\s*'([^']*)',\s*'([^']+)'\)", re.MULTILINE)

    def _migration_rules(self) -> list[tuple[str, str]]:
        text = self._MIGRATION.read_text(encoding="utf-8")
        return self._ROW.findall(text)

    def test_migration_file_exists(self):
        assert self._MIGRATION.is_file(), f"缺少回填迁移: {self._MIGRATION}"

    def test_rules_match_python_order_and_content(self):
        assert self._migration_rules() == list(_RULES)

    def test_migration_never_overwrites_existing_code(self):
        text = self._MIGRATION.read_text(encoding="utf-8")
        # 只补空：两条 UPDATE 的过滤条件都必须带 error_code IS NULL，
        # 否则会把上游更精确的包装码覆盖成推导值。
        assert text.count("t.error_code IS NULL") >= 2

    def test_migration_has_unattributed_fallback(self):
        text = self._MIGRATION.read_text(encoding="utf-8")
        assert UNATTRIBUTED_CODE in text
        assert "RAISE EXCEPTION" in text, "验证不变量必须硬失败，不能只 WARNING"
