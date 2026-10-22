"""Catalog ``risk_hint`` 与 log_collect 风险分级回归测试。

背景：同步官方命令清单会显著扩大 Catalog 覆盖面。若只更新 JSON 而不消费
``risk_hint``，新增的写命令会被默认判为 risk=1 自动执行（安全倒挂）。
本测试同时锁定两件事：

1. Catalog 中不得残留不可执行的 ``acli plugins ...`` 旧命名空间；
2. ``risk_hint=write/danger`` 的命令必须至少被判为 2，且只读命令不被误提升。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.tools.acli.classifier import _catalog_risk_level, classify_acli
from shared.resolution.catalog import acli_risk_hint, load_acli_catalog

CATALOG_PATH = Path(__file__).resolve().parents[3] / "shared" / "resolution" / "catalogs" / "acli_command_catalog.json"


class TestCatalogIntegrity:
    """Catalog 快照自身的一致性约束。"""

    def test_catalog_file_exists(self):
        assert CATALOG_PATH.is_file(), "acli 命令快照缺失"

    def test_no_legacy_plugins_namespace(self):
        commands = [str(item["command"]) for item in load_acli_catalog()]
        assert commands, "Catalog 不应为空"
        # acli 1.0.1 不存在 plugins 命名空间，残留即代表命令必然执行失败。
        assert [cmd for cmd in commands if cmd.startswith("acli plugins ")] == []

    def test_log_collect_commands_present(self):
        commands = {str(item["command"]) for item in load_acli_catalog()}
        assert {
            "acli log_collect create",
            "acli log_collect cancel",
            "acli log_collect progress",
        } <= commands

    def test_risk_hint_enum(self):
        invalid = [
            item["command"] for item in load_acli_catalog() if item.get("risk_hint") not in {"read", "write", "danger"}
        ]
        assert invalid == [], f"存在非法 risk_hint：{invalid[:5]}"


class TestRiskHintMatching:
    """最长前缀匹配语义。"""

    def test_danger_prefix(self):
        assert acli_risk_hint(["acli", "vm", "delete", "abc-123"]) == "danger"

    def test_read_prefix(self):
        assert acli_risk_hint(["acli", "log_collect", "progress"]) == "read"

    def test_unknown_prefix(self):
        assert acli_risk_hint(["acli", "definitely-not-a-command"]) is None

    def test_read_hint_does_not_downgrade(self):
        # read 只表示"无需提升"，分类器必须返回 None 继续走原有判定。
        assert _catalog_risk_level("acli log_collect progress") is None


class TestLogCollectRisk:
    """日志收集命令的风险语义。"""

    def test_progress_is_read_only(self):
        assert classify_acli("acli log_collect progress") == 1

    def test_create_requires_confirm(self):
        assert classify_acli("acli log_collect create -s Q20261022001 -m other") == 2

    def test_cancel_is_blocked(self):
        assert classify_acli("acli log_collect cancel") == 3


class TestSyncedCommandRisk:
    """同步新增命令不得落入自动执行。"""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("acli hardware usb blacklist add", 2),
            ("acli hardware usb blacklist delete", 3),
            ("acli network nic rdma whitelist set", 2),
            ("acli vm config pending delete abc-123", 3),
            ("acli acli sync", 2),
            ("acli network nic list", 1),
            # 回归：停机类命令保持既有 risk=2，不因 Catalog 同步被提升为 block。
            ("acli vm shutdown abc-123", 2),
            ("acli vm delete abc-123", 3),
        ],
    )
    def test_classification(self, command: str, expected: int):
        assert classify_acli(command) == expected

    def test_no_write_hint_command_is_auto(self):
        """属性断言：任何 write/danger 命令都不得被判为 risk=1。"""
        offenders = [
            item["command"]
            for item in load_acli_catalog()
            if item.get("risk_hint") in {"write", "danger"} and classify_acli(f"{item['command']} test-arg") == 1
        ]
        assert offenders == [], f"以下命令仍会自动执行：{offenders[:5]}"
